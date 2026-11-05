"""2.22: team member pages. Everything here is built from what the Team page already saved (team/team.json):
no Garage 61 request is ever made, and nothing here imports the Garage 61 client. Words only, speeds in Settings >
Units' unit. The window's Team page links each name to api/team/member?name=..., which calls page_for()."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from . import units
from .team import norm_name

GATE_S = 0.05            # a corner counts as "quicker" at this much or more
MIN_MPH = 1.5            # and a speed is named only when it differs by this much


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def _lap(t) -> str:
    if not _num(t) or t <= 0:
        return "no time"
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:06.3f}"


def _day(d) -> str:
    return str(d)[:10] if d else ""


def _first(name) -> str:
    s = str(name or "").strip()
    return s.split()[0] if s else "They"


def _corner_label(cid) -> str:
    s = str(cid or "").strip()
    digits = s.lstrip("TtCc ")
    return "Turn " + digits if digits.isdigit() else (s or "A corner")


def find(data, name):
    """(key, display name, is_me) for a name asked for, or None. Matches the saved members and the boards, ignoring
    case and accents; 'me' / 'you' / your own name is the user's own page."""
    if not isinstance(data, dict) or not str(name or "").strip():
        return None
    want = norm_name(name)
    me = data.get("me") if isinstance(data.get("me"), dict) else {}
    my_name = str(me.get("name") or "")
    if want in ("me", "you") or (my_name and want == norm_name(my_name)):
        return "me", my_name or "You", True
    for m in data.get("members") or []:
        if isinstance(m, dict) and m.get("name") and norm_name(m["name"]) == want:
            if m.get("is_me") or m.get("key") == "me":
                return "me", my_name or str(m["name"]), True
            return m.get("key"), str(m["name"]), False
    for c in data.get("combos") or []:
        for r in (c.get("board") or []) if isinstance(c, dict) else []:
            if isinstance(r, dict) and r.get("name") and norm_name(r["name"]) == want and r.get("key") is not None:
                return r["key"], str(r["name"]), r["key"] == "me"
    return None


def _row(c, key):
    if key is None:
        return None
    for r in c.get("board") or []:
        if isinstance(r, dict) and r.get("key") == key:
            return r
    return None


def _corner_lines(c, key, name):
    k = c.get("kings") if isinstance(c.get("kings"), dict) else None
    if not k or "me" not in (k.get("drivers") or []) or key not in (k.get("drivers") or []):
        return []
    out = []
    for z in k.get("corners") or []:
        t = z.get("times") or {}
        if not (_num(t.get("me")) and _num(t.get(key))) or t["me"] - t[key] < GATE_S:
            continue
        txt = f"{_corner_label(z.get('id'))}: {_first(name)} is {t['me'] - t[key]:.2f} s quicker"
        m = z.get("metrics") or {}
        a, b = (m.get("me") or {}).get("min_mph"), (m.get(key) or {}).get("min_mph")
        if _num(a) and _num(b) and b - a >= MIN_MPH:
            txt = (f"{_corner_label(z.get('id'))}: {_first(name)} carries {round(b - a)} mph more "
                   f"({round(b)} mph at the slowest point, you {round(a)})")
        out.append((t["me"] - t[key], txt))
    out.sort(key=lambda x: -x[0])
    return [x[1] for x in out[:6]]


def view(data, name, unit="mph") -> dict:
    """The member page as plain data. {"ok": False, "message"} for an unknown name."""
    hit = find(data, name)
    if hit is None:
        known = ([str(m["name"]) for m in data.get("members") or [] if isinstance(m, dict) and m.get("name")]
                 if isinstance(data, dict) else [])
        return {"ok": False, "message": "Debrief doesn't know a team member called “" + str(name or "").strip()[:60]
                + "”." + (" Known members: " + ", ".join(known) + "." if known else
                               " Refresh the Team page first so it can list your team.")}
    key, disp, is_me = hit
    combos = [c for c in (data.get("combos") or []) if isinstance(c, dict) and c.get("track")]
    tracks, unloaded, rows, corners = [], [], [], []
    best_ir, ir_day, last = None, "", ""
    for c in combos:
        label = (c.get("track_short") or c["track"]) + " · " + str(c.get("car") or "")
        if not c.get("loaded", True):
            if any(norm_name(w) == norm_name(disp) for w in (c.get("who") or [])) or (is_me and c.get("races")):
                unloaded.append({"track": c["track"], "car": c.get("car"), "label": label})
            continue
        r, mine = _row(c, key), _row(c, "me")
        if r is None:
            continue
        d = _day(r.get("date"))
        if d > last:
            last = d
        if _num(r.get("irating")) and d >= ir_day:
            best_ir, ir_day = round(r["irating"]), d
        item = {"label": label, "track": c["track"], "car": c.get("car"), "best": _lap(r.get("t")), "date": d}
        if not is_me:
            if mine is None or not _num(mine.get("t")):
                item["yours"], item["gap_text"] = "no time", "You haven't driven this."
            elif _num(r.get("t")):
                g = round(float(mine["t"]) - float(r["t"]), 3)
                item["yours"] = _lap(mine["t"])
                item["gap_text"] = (f"{_first(disp)} is {g:.2f} s quicker." if g > 0.0005 else
                                    f"You're {-g:.2f} s quicker." if g < -0.0005 else "Dead level.")
            corners += [(label, s) for s in _corner_lines(c, key, disp)]
        tracks.append(item)
        rows.append((d, item))
    rows.sort(key=lambda x: x[0], reverse=True)
    latest = [{"date": i["date"], "label": i["label"], "time": i["best"]} for d, i in rows if d][:8]
    why = {}
    if not tracks:
        why["tracks"] = ("Not loaded yet: open the track on the Team page and press Load this track."
                         if unloaded else "No laps for " + _first(disp) +
                         " are saved on the Team page yet. Press Refresh team data on the Team page.")
    if not is_me and tracks and not corners:
        why["corners"] = ("Not loaded yet: corner comparisons need both of your laps with telemetry. Open a track on the "
                          "Team page to load it. (Or nobody beats you by a clear margin in the corners that are loaded.)")
    why["consistency"] = ("Not available: the Team page keeps each driver's best lap only, so Debrief can't tell how steady "
                          + _first(disp) + " is lap to lap.")
    if not latest:
        why["latest"] = "No dated laps are saved yet."
    out = {"ok": True, "name": disp, "key": key, "is_me": is_me, "irating": best_ir, "tracks": tracks,
           "unloaded": unloaded, "corners": [{"track": a, "text": units.convert_text(b, unit)} for a, b in corners],
           "latest": latest, "last_lap": last or None, "refreshed": data.get("generated_at"), "why": why}
    return out


def cards(data) -> list:
    """The Members strip: one card per saved member: name, iRating, number of tracks, last lap date."""
    out = []
    for m in (data.get("members") or []) if isinstance(data, dict) else []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        v = view(data, m["name"])
        out.append({"name": str(m["name"]), "is_me": bool(v.get("is_me")), "irating": v.get("irating"),
                    "tracks": len(v.get("tracks") or []), "last_lap": v.get("last_lap"), "problem": m.get("problem")})
    return out


CSS = ("body{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:24px 16px 48px;color:#161a1f;background:#f3f4f6}"
       "@media(prefers-color-scheme:dark){body{color:#eceff2;background:#0e1114}.c{background:#161a1e;border-color:#262b31}.m{color:#8b949d}}"
       ":root[data-theme=dark] body{color:#eceff2;background:#0e1114}:root[data-theme=light] body{color:#161a1f;background:#f3f4f6}"
       "h1{margin:4px 0;font-size:34px}h2{font-size:18px;margin:0 0 8px}.c{background:#fff;border:1px solid #e3e6ea;border-radius:6px;"
       "padding:16px;margin:16px 0}.m{color:#68717b;font-size:14px}table{border-collapse:collapse;width:100%}"
       "td,th{text-align:left;padding:6px 8px;border-top:1px solid #8884;font-size:14px}th{border-top:none}a{color:#1f6cc4}"
       "button{font:inherit;padding:4px 12px;border-radius:6px;border:1px solid #1f6cc4;background:#1f6cc4;color:#fff;cursor:pointer}")


def html(v) -> str:
    e = escape
    if not v.get("ok"):
        return _page("Team member", f"<h1>Team member</h1><div class=c><p>{e(v['message'])}</p>"
                     "<p><a href='../../lib/team/index.html'>Back to the Team page</a></p></div>")
    h = [f"<p class=m><a href='../../lib/team/index.html'>← Team page</a></p><h1>{e(v['name'])}{' (you)' if v['is_me'] else ''}</h1>",
         "<p class=m>" + ("iRating " + str(v["irating"]) if v.get("irating") else "iRating not known")
         + " · " + ("Last lap " + e(v["last_lap"]) if v.get("last_lap") else "No lap date saved")
         + " · " + ("Last refreshed " + e(_day(v["refreshed"])) if v.get("refreshed") else "Not refreshed yet") + "</p>",
         "<div class=c><h2>Best laps by track and car</h2>"]
    if v["tracks"]:
        vs = not v["is_me"]
        h.append("<table><tr><th>Track and car</th><th>" + ("Their best" if vs else "Your best") + "</th>"
                 + ("<th>Your best</th><th>Gap</th>" if vs else "") + "</tr>")
        for t in v["tracks"]:
            h.append(f"<tr><td>{e(t['label'])}</td><td>{e(t['best'])}</td>"
                     + (f"<td>{e(t.get('yours', ''))}</td><td>{e(t.get('gap_text', ''))}</td>" if vs else "") + "</tr>")
        h.append("</table>")
    else:
        h.append("<p class=m>" + e(v["why"].get("tracks", "")) + "</p>")
    for u in v["unloaded"]:
        h.append(f"<p class=m>{e(u['label'])}: not loaded yet. <button data-t=\"{e(u['track'], True)}\" "
                 f"data-c=\"{e(str(u['car']), True)}\">Load this track</button></p>")
    h.append("</div>")
    if not v["is_me"]:
        h.append("<div class=c><h2>Corners where they're quicker than you</h2>")
        h.append("<ul>" + "".join(f"<li><span class=m>{e(c['track'])}</span> — {e(c['text'])}</li>" for c in v["corners"]) + "</ul>"
                 if v["corners"] else "<p class=m>" + e(v["why"].get("corners", "No corner comparison to show.")) + "</p>")
        h.append("</div>")
    h.append("<div class=c><h2>How steady</h2><p class=m>" + e(v["why"]["consistency"]) + "</p></div>")
    h.append("<div class=c><h2>Latest laps</h2>")
    h.append("<table><tr><th>Date</th><th>Track and car</th><th>Time</th></tr>" + "".join(
        f"<tr><td>{e(r['date'])}</td><td>{e(r['label'])}</td><td>{e(r['time'])}</td></tr>" for r in v["latest"]) + "</table>"
        if v["latest"] else "<p class=m>" + e(v["why"].get("latest", "")) + "</p>")
    h.append("</div><p class=m id=msg></p><script>document.querySelectorAll('button[data-t]').forEach(function(b){b.onclick=function(){"
             "try{parent.postMessage({type:'debrief-team-load',track:b.dataset.t,car:b.dataset.c},'*')}catch(x){}"
             "document.getElementById('msg').textContent='Loading started. Come back to this page when the Team page says it is done.'}})</script>")
    return _page(v["name"], "".join(h))


def _page(title, body) -> str:
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")


def page_for(library_folder, name, unit="mph") -> str:
    """The member page from the saved team.json only."""
    try:
        data = json.loads((Path(library_folder) / "team" / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return html({"ok": False, "message": "The Team page hasn't been built yet. Open Team and press Refresh team data."})
    return html(view(data, name, unit))
