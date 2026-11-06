"""2.25: "Save as picture" engagement cards for the Team hub and member pages.

This file only decides the WORDS and NUMBERS on each card (a "spec"): headline, one hero number, a few rows and a
punchy line made by fixed rules (no AI). The picture itself is drawn in the page by templates/cards.js on a canvas,
the same way the review's Share card is, and saved by the window into <library>/shares. Pure functions, no I/O,
no Garage 61 request. Team and driver names come only from the saved team data; nothing is built in.

Every builder returns a spec even when data is missing (a plain "nothing to show yet" card), never raises.
"""
from __future__ import annotations

import re

from . import brand, units

SIZE = (1200, 675)
KINDS = ("leaderboard", "duel", "corner", "member", "best", "learn")
MAX_COMBOS = 60
MAX_CORNERS = 24
MAX_SPECS = 600
LABELS = {"leaderboard": "leaderboard", "duel": "head-to-head", "corner": "corner-king", "member": "member",
          "best": "team-best", "learn": "learn-from"}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def lap(t) -> str:
    if not _num(t) or t <= 0:
        return "no time"
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def slug(*parts, limit=70) -> str:
    s = "-".join(str(p) for p in parts if p)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:limit].strip("-")


def file_name(kind, *parts) -> str:
    """A safe name such as spa-gt3-leaderboard.png: lower-case letters, digits and dashes only."""
    base = slug(*parts) or "debrief"
    return f"{base}-{LABELS.get(kind, 'card')}.png"


def _first(name) -> str:
    s = str(name or "").strip()
    return s.split()[0] if s else "They"


def _date(d) -> str:
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)", str(d or ""))
    if not m:
        return ""
    mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(m.group(2)) - 1:int(m.group(2))]
    return f"{mon[0]} {int(m.group(3))}" if mon else ""


def team_name(data) -> str:
    """The Garage 61 team's name from the saved data; empty when it isn't there."""
    if not isinstance(data, dict):
        return ""
    for v in (data.get("team_name"), (data.get("group") or {}).get("name") if isinstance(data.get("group"), dict) else None,
              (data.get("team") or {}).get("name") if isinstance(data.get("team"), dict) else None):
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _base(data, kind, layout, kicker) -> dict:
    return {"kind": kind, "layout": layout, "w": SIZE[0], "h": SIZE[1], "team": team_name(data), "kicker": kicker,
            "headline": "", "sub": "", "hero": None, "rows": [], "cols": [], "stats": [], "line": "",
            "brand": brand.BYLINE, "empty": False, "file": file_name(kind)}


def _empty(data, kind, layout, kicker, what) -> dict:
    s = _base(data, kind, layout, kicker)
    s.update(empty=True, headline=what, line="Nothing to show on this card yet. Press Refresh team data, then try again.")
    return s


def _me_name(data) -> str:
    me = data.get("me") if isinstance(data, dict) and isinstance(data.get("me"), dict) else {}
    return str(me.get("name") or "You")


def _combos(data) -> list:
    return [c for c in (data.get("combos") or [])[:MAX_COMBOS] if isinstance(c, dict) and c.get("track")] \
        if isinstance(data, dict) else []


def _board(c) -> list:
    return [r for r in (c.get("board") or []) if isinstance(r, dict) and _num(r.get("t"))]


def _track(c) -> str:
    return str(c.get("track_short") or c.get("track") or "this track")


def _name_of(data, c, key) -> str:
    for r in c.get("board") or []:
        if isinstance(r, dict) and r.get("key") == key and r.get("name"):
            return str(r["name"])
    kn = ((c.get("kings") or {}).get("names") or {}) if isinstance(c.get("kings"), dict) else {}
    return str(kn.get(key) or (_me_name(data) if key == "me" else key))


def leaderboard(data, c, kicker="TEAM LEADERBOARD", title=None) -> dict:
    """One track and car: the top five, a highlighted #1."""
    rows = sorted(_board(c), key=lambda r: r["t"]) if isinstance(c, dict) else []
    if not rows:
        return _empty(data, "leaderboard", "board", kicker, _track(c) if isinstance(c, dict) else "Leaderboard")
    s = _base(data, "leaderboard", "board", kicker)
    top = rows[0]
    s.update(headline=_track(c), sub=str(title or c.get("car") or ""),
             file=file_name("leaderboard", _track(c), c.get("car")))
    s["hero"] = {"label": "TEAM BEST", "value": lap(top["t"]), "sub": str(top.get("name") or "Driver")}
    for i, r in enumerate(rows[:5]):
        gap = r["t"] - top["t"]
        ir = r.get("irating")
        s["rows"].append({"pos": i + 1, "name": str(r.get("name") or "Driver"), "value": lap(r["t"]),
                          "gap": "LEADER" if i == 0 else f"+{gap:.3f}", "ir": f"{round(ir)}" if _num(ir) else "",
                          "date": _date(r.get("date")), "hl": i == 0})
    if len(rows) > 1:
        g = rows[1]["t"] - top["t"]
        s["line"] = (f"{g:.3f} s to find at {_track(c)}. Who's next?" if g > 0.0005 else
                     f"{_first(rows[0].get('name'))} and {_first(rows[1].get('name'))} are level at {_track(c)}. Who breaks it?")
    else:
        s["line"] = f"Only {_first(top.get('name'))} has a time at {_track(c)} so far. Who's next?"
    return s


def best_card(data, c) -> dict:
    """The newest team best on a board: the leader's lap, and by how much it beat the next driver."""
    rows = sorted(_board(c), key=lambda r: r["t"]) if isinstance(c, dict) else []
    if not rows:
        return _empty(data, "best", "best", "NEW TEAM BEST", "Team best")
    top = rows[0]
    s = _base(data, "best", "best", "NEW TEAM BEST")
    nm = str(top.get("name") or "A driver")
    s.update(headline=f"{nm} set a new team best", sub=f"{_track(c)} · {c.get('car') or ''}".strip(" ·"),
             file=file_name("best", _track(c), c.get("car"), nm))
    s["hero"] = {"label": "LAP TIME", "value": lap(top["t"]), "sub": f"{nm} · {_date(top.get('date'))}".strip(" ·")}
    if len(rows) > 1:
        g = rows[1]["t"] - top["t"]
        s["stats"] = [{"label": "AHEAD OF", "value": _first(rows[1].get("name")), "sub": f"{g:.3f} s clear"}]
        s["line"] = f"{g:.3f} s clear of {_first(rows[1].get('name'))}. Think you can beat {lap(top['t'])}?"
    else:
        s["line"] = f"{lap(top['t'])} is the time to beat at {_track(c)}. Who's coming for it?"
    return s


def _corner_label(cid) -> str:
    s = str(cid or "").strip()
    d = s.lstrip("TtCc ")
    return f"Turn {d}" if d.isdigit() else (s or "Corner")


def corner_king(data, c, corner_id=None, unit="mph") -> dict:
    """Who owns one corner and by how much (time, and speed in Settings > Units' unit when known)."""
    k = c.get("kings") if isinstance(c, dict) and isinstance(c.get("kings"), dict) else None
    zs = [z for z in (k.get("corners") or []) if isinstance(z, dict) and _num(z.get("margin"))] if k else []
    if not zs:
        return _empty(data, "corner", "corner", "CORNER KING", _track(c) if isinstance(c, dict) else "Corner king")
    z = next((x for x in zs if str(x.get("id")) == str(corner_id)), None) or max(zs, key=lambda x: x["margin"])
    nm = lambda key: _name_of(data, c, key)  # noqa: E731
    king, second = z.get("king"), z.get("second")
    s = _base(data, "corner", "corner", "CORNER KING")
    s.update(headline=_corner_label(z.get("id")), sub=f"{_track(c)} · {c.get('car') or ''}".strip(" ·"),
             file=file_name("corner", _track(c), c.get("car"), z.get("id")))
    ul = units.label(unit)
    met = z.get("metrics") if isinstance(z.get("metrics"), dict) else {}

    def mph(key):
        v = (met.get(key) or {}).get("min_mph")
        return units.speed(v, "mph", unit) if _num(v) else None
    kv, sv = mph(king), mph(second)
    s["hero"] = {"label": "CORNER KING", "value": nm(king), "sub": f"{z['margin']:.3f} s clear of {_first(nm(second))}"}
    if kv is not None and sv is not None and abs(kv - sv) >= 0.5:
        d = kv - sv
        s["stats"] = [{"label": "SPEED AT THE SLOWEST POINT", "value": f"{'+' if d > 0 else '-'}{abs(d):.0f} {ul}",
                       "sub": f"{kv:.0f} {ul} against {sv:.0f} {ul}"}]
    for i, key in enumerate(sorted((z.get("times") or {}), key=lambda q: z["times"][q])[:4]):
        v = mph(key)
        s["rows"].append({"pos": i + 1, "name": nm(key), "value": f"{z['times'][key]:.3f} s",
                          "gap": "KING" if i == 0 else f"+{z['times'][key] - z['times'][king]:.3f}",
                          "ir": f"{v:.0f} {ul}" if v is not None else "", "date": "", "hl": i == 0})
    s["line"] = f"{_first(nm(king))} owns {_corner_label(z.get('id'))} at {_track(c)} by {z['margin']:.3f} s. Think you can take it?"
    return s


def _pair(c, ka, kb):
    b = {r.get("key"): r for r in _board(c)}
    return (b.get(ka), b.get(kb)) if ka in b and kb in b else (None, None)


def duel(data, key_a, key_b, combo=None) -> dict:
    """"A vs B at <track>": lap times, gap, corners won each, and who is ahead over every shared track."""
    cs = _combos(data)
    shared = [c for c in cs if all(_pair(c, key_a, key_b))]
    if not shared:
        return _empty(data, "duel", "duel", "HEAD TO HEAD", "Head to head")
    c = combo if combo in shared else shared[0]
    ra, rb = _pair(c, key_a, key_b)
    na, nb = str(ra.get("name") or _me_name(data)), str(rb.get("name") or "Teammate")
    ahead_a = sum(1 for x in shared if _pair(x, key_a, key_b)[0]["t"] < _pair(x, key_a, key_b)[1]["t"])
    ahead_b = sum(1 for x in shared if _pair(x, key_a, key_b)[0]["t"] > _pair(x, key_a, key_b)[1]["t"])
    ca = cb = None
    k = c.get("kings") if isinstance(c.get("kings"), dict) else None
    if k and key_a in (k.get("drivers") or []) and key_b in (k.get("drivers") or []):
        zs = [z for z in k.get("corners") or [] if isinstance(z, dict) and isinstance(z.get("times"), dict)
              and _num(z["times"].get(key_a)) and _num(z["times"].get(key_b))]
        ca = sum(1 for z in zs if z["times"][key_a] < z["times"][key_b])
        cb = sum(1 for z in zs if z["times"][key_b] < z["times"][key_a])
    gap = ra["t"] - rb["t"]
    s = _base(data, "duel", "duel", "HEAD TO HEAD")
    s.update(headline=f"{na} vs {nb}", sub=f"{_track(c)} · {c.get('car') or ''}".strip(" ·"),
             file=file_name("duel", _track(c), c.get("car"), na, nb))
    lead = na if gap < 0 else nb
    s["hero"] = {"label": "GAP", "value": f"{abs(gap):.3f} s" if abs(gap) > 0.0005 else "LEVEL",
                 "sub": f"{lead} is quicker" if abs(gap) > 0.0005 else "dead level"}
    s["cols"] = [{"name": na, "value": lap(ra["t"]), "sub": f"{ca} corner{'s' if ca != 1 else ''} won" if ca is not None else "corners not compared",
                  "hl": gap < 0}, {"name": nb, "value": lap(rb["t"]), "sub": f"{cb} corner{'s' if cb != 1 else ''} won" if cb is not None else "corners not compared",
                                   "hl": gap > 0}]
    n = len(shared)
    overall = (f"{na} is ahead at {ahead_a} of {n} tracks, {nb} at {ahead_b}" if ahead_a != ahead_b else
               f"Level over {n} track{'s' if n != 1 else ''}: {ahead_a} each")
    s["stats"] = [{"label": "OVERALL", "value": f"{ahead_a} - {ahead_b}", "sub": overall}]
    if abs(gap) > 0.0005:
        s["line"] = (f"{abs(gap):.3f} s between them at {_track(c)}. {_first(nb if gap < 0 else na)} wants a rematch."
                     if n == 1 else f"{abs(gap):.3f} s at {_track(c)}, {ahead_a} - {ahead_b} overall. Who wants a rematch?")
    else:
        s["line"] = f"Dead level at {_track(c)}. Somebody has to find a tenth."
    return s


def learn_card(data, c, row) -> dict:
    tm = row.get("teammate") if isinstance(row, dict) and isinstance(row.get("teammate"), dict) else {}
    nm = str(tm.get("name") or tm.get("key") or "")
    if not nm or not _num(row.get("gap_s")):
        return _empty(data, "learn", "corner", "WHO TO LEARN FROM", _track(c))
    s = _base(data, "learn", "corner", "WHO TO LEARN FROM")
    s.update(headline=_corner_label(row.get("corner")), sub=f"{_track(c)} · {c.get('car') or ''}".strip(" ·"),
             file=file_name("learn", _track(c), c.get("car"), row.get("corner"), nm))
    s["hero"] = {"label": "LEARN FROM", "value": nm, "sub": f"{row['gap_s']:.2f} s quicker through here"}
    if row.get("text"):
        s["stats"] = [{"label": "WHAT THEY DO", "value": "", "sub": str(row["text"])[:160]}]
    s["line"] = f"{row['gap_s']:.2f} s to find at {_corner_label(row.get('corner'))}. Ask {_first(nm)} how."
    return s


def member(data, name, unit="mph", lib=None) -> dict:
    """A driver's snapshot: tracks, best laps, how steady, last raced."""
    from . import members
    v = members.view(data, name, unit)
    if not v.get("ok"):
        return _empty(data, "member", "member", "DRIVER", str(name or "Driver"))
    disp = v["name"]
    if callable(lib):
        try:
            lib = lib(disp)
        except Exception:  # noqa: BLE001
            lib = None
    s = _base(data, "member", "member", "DRIVER SNAPSHOT")
    s.update(headline=disp, sub=(f"iRating {v['irating']}" if v.get("irating") else "iRating not known"),
             file=file_name("member", disp))
    tracks = v.get("tracks") or []
    steady = "Not tracked yet"
    if isinstance(lib, dict) and lib.get("found") and lib.get("consistency"):
        steady = re.split(r"(?<=[.!?])\s", str(lib["consistency"]).strip())[0][:70]
    s["stats"] = [{"label": "TRACKS", "value": str(len(tracks)), "sub": "with a best lap"},
                  {"label": "LAST RACED", "value": _date(v.get("last_lap")) or "-", "sub": ""},
                  {"label": "HOW STEADY", "value": "", "sub": steady}]
    best = sorted(tracks, key=lambda t: t.get("date") or "", reverse=True)[:4]
    for i, t in enumerate(best):
        s["rows"].append({"pos": i + 1, "name": str(t.get("label") or ""), "value": str(t.get("best") or ""), "gap": "",
                          "ir": "", "date": _date(t.get("date")), "hl": i == 0})
    if tracks:
        s["hero"] = {"label": "LAPS ON FILE", "value": str(len(tracks)), "sub": "tracks and cars"}
        s["line"] = (f"{_first(disp)} has times at {len(tracks)} track{'s' if len(tracks) != 1 else ''}. "
                     f"Think you can beat them?")
    else:
        s["line"] = f"No laps for {_first(disp)} are saved yet. Press Refresh team data."
        s["empty"] = True
    return s


def build_all(data, unit="mph", lib=None) -> dict:
    """Every card the Team page can offer, by id: lb:i leaderboard, nb:i new best, kg:i[:corner] corner king,
    dl:key:i duel, mb:name member, wk:s:w weekly board, ss:key season duel, ln:i:j learn. Never raises."""
    out: dict = {}

    def add(k, fn):
        if len(out) >= MAX_SPECS:
            return
        try:
            out[k] = fn()
        except Exception:  # noqa: BLE001  (a card that can't be made is just not offered)
            pass
    if not isinstance(data, dict):
        return out
    cs = [c for c in (data.get("combos") or [])[:MAX_COMBOS] if isinstance(c, dict)]
    for i, c in enumerate(cs):
        if not c.get("track"):
            continue
        if _board(c):
            add(f"lb:{i}", lambda c=c: leaderboard(data, c))
            add(f"nb:{i}", lambda c=c: best_card(data, c))
            for r in _board(c):
                if r.get("key") not in (None, "me") and "me" in {x.get("key") for x in _board(c)}:
                    add(f"dl:{r['key']}:{i}", lambda c=c, r=r, i=i: duel(data, "me", r["key"], c))
        k = c.get("kings") if isinstance(c.get("kings"), dict) else None
        if k:
            add(f"kg:{i}", lambda c=c: corner_king(data, c, None, unit))
            for z in (k.get("corners") or [])[:MAX_CORNERS]:
                if isinstance(z, dict) and z.get("id") is not None:
                    add(f"kg:{i}:{z['id']}", lambda c=c, z=z: corner_king(data, c, z["id"], unit))
        for j, row in enumerate((c.get("learn") or [])[:5]):
            if isinstance(row, dict):
                add(f"ln:{i}:{j}", lambda c=c, row=row: learn_card(data, c, row))
    seen = set()
    for c in cs:
        for r in _board(c):
            k = r.get("key")
            if k not in (None, "me") and k not in seen and "me" in {x.get("key") for x in _board(c)}:
                seen.add(k)
                add(f"dh:{k}", lambda k=k: duel(data, "me", k))
    for m in data.get("members") or []:
        if isinstance(m, dict) and m.get("name"):
            add(f"mb:{m['name']}", lambda m=m: member(data, m["name"], unit, lib))
    W = data.get("weekly") if isinstance(data.get("weekly"), dict) else {}
    for si, sr in enumerate(W.get("series") or []):
        if not isinstance(sr, dict):
            continue
        for wi, w in enumerate(sorted([w for w in sr.get("weeks") or [] if isinstance(w, dict) and _num(w.get("week"))],
                                      key=lambda w: -w["week"])[:3]):
            add(f"wk:{si}:{wi}", lambda sr=sr, w=w: leaderboard(
                data, {"track": w.get("track") or "Track", "track_short": w.get("track"), "car": sr.get("name") or "",
                       "board": [r for r in w.get("board") or [] if isinstance(r, dict)]},
                f"THIS WEEK · WEEK {int(w['week'])}"))
    for p in W.get("h2h") or []:
        if isinstance(p, dict) and p.get("key") not in (None, "me") and _num(p.get("shared_weeks")):
            add(f"ss:{p['key']}", lambda p=p: season_duel(data, p))
    return out


def season_duel(data, p) -> dict:
    nm, me = str(p.get("name") or p.get("key") or "Teammate"), _me_name(data)
    sh = int(p.get("shared_weeks") or 0)
    be = int(p.get("behind") or 0)
    s = _base(data, "duel", "duel", "SEASON HEAD TO HEAD")
    s.update(headline=f"{me} vs {nm}", sub=f"{sh} shared week{'s' if sh != 1 else ''} this season",
             file=file_name("duel", "season", me, nm))
    if not sh:
        s.update(empty=True, line="No shared weeks yet. Set a lap in the same week and this fills in.")
        return s
    md = p.get("median_gap_pct")
    s["hero"] = {"label": "WEEKS WON", "value": f"{sh - be} - {be}", "sub": f"{me} - {nm}"}
    s["cols"] = [{"name": me, "value": str(sh - be), "sub": "weeks quicker", "hl": sh - be > be},
                 {"name": nm, "value": str(be), "sub": "weeks quicker", "hl": be > sh - be}]
    if _num(md):
        s["stats"] = [{"label": "MEDIAN GAP", "value": f"{abs(md):.2f} %", "sub": f"{nm} is quicker" if md > 0 else f"{me} is quicker"}]
    s["line"] = (f"{nm} leads {be} - {sh - be} this season. Time for a comeback?" if be > sh - be else
                 f"{me} leads {sh - be} - {be} this season. Who wants it back?" if sh - be > be else
                 f"Level at {be} - {be} this season. Who blinks first?")
    return s
