"""Local HTTP server for the Debrief desktop window.

Bound to 127.0.0.1 on a random free port. Every URL starts with a random per-session
secret, so other pages on this PC can't reach the API. POSTs also need a custom header
(which a cross-origin page can't send without a CORS preflight this server never answers).
"""
from __future__ import annotations

import json
import math
import base64
import hashlib
import os
import secrets
import platform
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .. import app, library
from ..g61 import Garage61Error
from . import settings_io
from . import carsetup    # 2.15 Car and setup
from . import racing      # 2.16 Racing
from . import carcompare as carcompare_page     # 2.18 Car vs car
from . import coach2      # Coach 2.0: voice packs, device, cue log, rehearsal

UI = Path(__file__).resolve().parent / "ui"
ICON_PNG = Path(__file__).resolve().parent / "icon.png"
FALLBACK_TOKENS = """:root{
  --bg:#f3f4f6; --panel:#ffffff; --ink:#161a1f; --ink2:#47505a; --muted:#68717b;
  --line:#e3e6ea; --axis:#c4c9d0; --band:#f2f4f6; --track:#dfe3e8; --hi:#eef3fb;
  --a:#1f6cc4; --b:#eb6834; --mid:#d6dade; --pb:#6250d6; --pbbg:#ece9fb;
  --warn:#946000; --good:#0a7a2f; --bad:#c43c3c;
  --bt:#b8461a; --on-a:#ffffff;
  --h1:#e6effb; --h7:#0d366b;
  --shadow:0 8px 28px rgba(20,26,34,.14);
  --fg:var(--ink); --fg2:var(--ink2); --grid:var(--line); --me:var(--a); --ref:var(--b); --lose:var(--bad); --gain:var(--a);
  --display:"Barlow Condensed","Arial Narrow","Roboto Condensed",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
  --t-xs:11.5px; --t-sm:13px; --t-md:15px; --t-lg:18px; --t-xl:24px; --t-2xl:38px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0e1114; --panel:#161a1e; --ink:#eceff2; --ink2:#b8bfc7; --muted:#8b949d;
  --line:#262b31; --axis:#3b424a; --band:#1b2025; --track:#2b3138; --hi:#1b2430;
  --a:#3987e5; --b:#d95926; --mid:#3b4047; --pb:#9085e9; --pbbg:#262240;
  --warn:#e2a83a; --good:#3dbb63; --bad:#e66767; --bt:#e07a4f; --on-a:#0e1114;
  --h1:#1a2430; --h7:#9ec5f4; --shadow:0 8px 28px rgba(0,0,0,.5); color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0e1114; --panel:#161a1e; --ink:#eceff2; --ink2:#b8bfc7; --muted:#8b949d;
  --line:#262b31; --axis:#3b424a; --band:#1b2025; --track:#2b3138; --hi:#1b2430;
  --a:#3987e5; --b:#d95926; --mid:#3b4047; --pb:#9085e9; --pbbg:#262240;
  --warn:#e2a83a; --good:#3dbb63; --bad:#e66767; --bt:#e07a4f; --on-a:#0e1114;
  --h1:#1a2430; --h7:#9ec5f4; --shadow:0 8px 28px rgba(0,0,0,.5); color-scheme:dark}
"""
LIB_TYPES = {".html", ".json", ".png", ".svg", ".css", ".js"}
# Fixed types: never Windows' registry (mimetypes reads HKCR, where .js is often text/plain, and with
# nosniff Edge then refuses to run app.js and the window opens blank).
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2"}
# 2.7.0 (M9): the three faces ship with Debrief (ui/fonts/, SIL OFL) and are served at static/fonts/, so
# the window and the library pages never call fonts.googleapis.com. Only these names are served.
FONTS = UI / "fonts"
FONT_FILE_OK = re.compile(r"^[a-z0-9-]+\.(woff2|css)$")
FONTS_CSS = "static/fonts/fonts.css"
# Library pages carry no font link of their own (opened from disk they use the system fonts); the local
# stylesheet link is added as a page is served, and the Google Fonts links of pages saved before 2.7.0 are
# replaced by it, so no page ever calls out.
_FONT_LINK = re.compile(r'<link\b[^>]*\bhref="(?:https://fonts\.googleapis\.com/[^"]*|/[A-Za-z0-9_-]+/static/fonts/fonts\.css)"[^>]*>', re.I)
_FONT_PRECONNECT = re.compile(r'<link\b[^>]*\brel="preconnect"[^>]*\bhref="https://fonts\.(?:googleapis|gstatic)\.com"[^>]*>\s*', re.I)
_FIRST_STYLE = re.compile(r"<(?:style|link)\b", re.I)
# Library pages (review, library, prep, team pages, or any .html someone drops in the folder) run in an
# opaque origin: their scripts work, but they can't call the API, read the window or change settings.
# connect-src 'none' matches the policy in the templates' own <meta> tag: library pages never use the network
# (their data is inline), so a planted page can't send anything anywhere either.
LIB_CSP = ("sandbox allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads; "
           "connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
# The iframe that shows library pages (ui/index.html) must carry the same flags:
FRAME_SANDBOX = "allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads"
# Added to every library page as it is served: tells the window which page is showing (the window can't
# look inside a sandboxed page), applies the theme it sends, and opens outside links in the browser.
FRAME_BRIDGE = ("<script>/* debrief-frame-bridge */(function(){if(parent===window)return;"
                "var P=new URL(location.href).origin;"  # the page's own origin is opaque ("null") in the sandbox
                "function say(){try{parent.postMessage({type:'debrief-frame',href:location.href,"
                "title:String(document.title||'').slice(0,200)},P)}catch(e){}}"
                "addEventListener('message',function(e){if(e.source!==parent)return;var m=e.data;"
                "if(!m||m.type!=='debrief-theme')return;var r=document.documentElement;"
                "if(m.theme==='light'||m.theme==='dark')r.setAttribute('data-theme',m.theme);"
                "else r.removeAttribute('data-theme')});"
                "document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&"
                "e.target.closest('a[href]');if(!a)return;var u;try{u=new URL(a.getAttribute('href'),"
                "document.baseURI)}catch(x){return}if(u.origin!==P){a.target='_blank';a.rel='noreferrer'}},true);"
                # 2.8: the sidebar owns Progress / Season / Skills / Goals, and these keys work inside the page too (2.12: Ctrl+1-9, 2.13: Ctrl+0)
                "document.body&&document.body.classList.add('in-shell');"
                "addEventListener('keydown',function(e){var k=String(e.key).toLowerCase(),m=null,t=e.target;"
                "if((e.ctrlKey||e.metaKey)&&!e.altKey&&!e.shiftKey&&/^[0-9]$/.test(k))m='page'+k;"
                "else if(!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.shiftKey&&(k==='j'||k==='k')&&"
                "!(t&&t.closest&&t.closest('input,select,textarea,[contenteditable]')))m=k==='j'?'prev':'next';"
                "if(!m)return;e.preventDefault();try{parent.postMessage({type:'debrief-key',key:m},P)}catch(x){}});"
                "addEventListener('hashchange',say);if(document.readyState==='loading')"
                "addEventListener('DOMContentLoaded',say);else say()})();</script>")
# In the <head>, so the tabs are gone before the page first paints. Only when shown inside the window: a page
# opened on its own keeps them. Pages can style on .in-shell (on <html> from here, on <body> from the bridge).
FRAME_EARLY = ("<script>/* debrief-in-shell */if(parent!==window)document.documentElement.classList.add('in-shell')"
               "</script><style>.in-shell .tabs{display:none!important}</style>")
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def ui_csp(html: str) -> str:
    """A strict policy for the window itself. Inline <script> blocks in index.html are allowed by hash,
    so editing them never needs a change here; inline event handlers (onclick=...) are not allowed."""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode() + "'"
                      for m in _INLINE_SCRIPT.findall(html))
    return ("default-src 'self'; script-src 'self'" + (" " + hashes if hashes else "") + "; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
            "worker-src 'self' blob:; "   # the heartbeat worker (app.js) is a blob: script
            "object-src 'none'; base-uri 'none'; form-action 'self'")


def with_bridge(html: bytes) -> bytes:
    tag = FRAME_BRIDGE.encode("utf-8")
    low = html.lower()
    head = low.find(b"</head>")
    if head >= 0:
        early = FRAME_EARLY.encode("utf-8")
        html = html[:head] + early + html[head:]
        low = low[:head] + early.lower() + low[head:]
    i = low.rfind(b"</body>")
    return html[:i] + tag + html[i:] if i >= 0 else html + tag


def with_fonts(html: bytes, secret: str) -> bytes:
    """A library page as served: a stylesheet link to this server's bundled fonts goes in its <head>
    (an absolute path, since review, prep, team and library pages sit at different depths), before the
    page's own styles. The Google Fonts links of pages saved before 2.7.0 are replaced by it, so no page
    ever calls out. Only the <head> is looked at: a page's own data (the inline JSON) is never touched."""
    end = html.lower().find(b"</head>")
    if end < 0:
        return html
    head = html[:end].decode("utf-8", "surrogateescape")
    local = f'<link rel="stylesheet" href="/{secret}/{FONTS_CSS}">'
    head = _FONT_PRECONNECT.sub("", head)
    done = False

    def swap(m):
        nonlocal done
        if done:
            return ""
        done = True
        return local
    head = _FONT_LINK.sub(swap, head)
    if not done:
        m = _FIRST_STYLE.search(head)
        head = head[:m.start()] + local + "\n" + head[m.start():] if m else head + local + "\n"
    return head.encode("utf-8", "surrogateescape") + html[end:]
REVIEW_DIR_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
GOAL_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# race result fields: (kind, min, max). results.save validates again; this is the server's own gate.
RESULT_FIELDS = {"start_pos": (int, 1, 200), "finish_pos": (int, 1, 200), "field_size": (int, 1, 200),
                 "incidents": (int, 0, 999), "irating_before": (int, 0, 20000), "irating_after": (int, 0, 20000),
                 "sr_before": (float, 0.0, 4.99), "sr_after": (float, 0.0, 4.99), "sof": (int, 0, 20000),
                 "laps_led": (int, 0, 2000)}


RESULT_DERIVED = ("irating_change", "sr_change", "positions_gained", "positions_lap1")


def clean_result_fields(raw) -> dict:
    """Validate the race-result form. Empty boxes are left out. Raises ValueError with a friendly message.
    Uses results.validate itself, so the server and results follow exactly the same rules (2.4.0 review L1;
    spec §2.5); the derived fields are left for results.save to work out. The local rules below are only a
    fallback for when the results module can't be loaded."""
    if not isinstance(raw, dict):
        raise ValueError("Bad request.")
    if raw.get("notes") not in (None, "") and not isinstance(raw.get("notes"), str):
        raise ValueError("Notes must be text.")
    results = app.optional_module("results")
    if results is not None and callable(getattr(results, "validate", None)):
        out = results.validate(raw)
        for k in RESULT_DERIVED:
            out.pop(k, None)
        return out
    return _clean_result_fields_local(raw)


def _clean_result_fields_local(raw) -> dict:
    out = {}
    out = {}
    labels = {"start_pos": "Start position", "finish_pos": "Finish position", "field_size": "Cars in the race",
              "incidents": "Incidents", "irating_before": "iRating before", "irating_after": "iRating after",
              "sr_before": "Safety rating before", "sr_after": "Safety rating after", "sof": "Strength of field",
              "laps_led": "Laps led"}
    for k, (kind, lo, hi) in RESULT_FIELDS.items():
        v = raw.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, bool):
            raise ValueError(f"{labels[k]} must be a number.")
        try:
            num = float(str(v).strip().replace(",", ".")) if isinstance(v, str) else float(v)   # as results._value
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{labels[k]} must be a number.")
        if not math.isfinite(num):  # NaN, Infinity, 1e400
            raise ValueError(f"{labels[k]} must be a number.")
        if not lo <= num <= hi:
            raise ValueError(f"{labels[k]} must be between {lo} and {hi}.")
        if kind is int:
            if num != int(num):
                raise ValueError(f"{labels[k]} must be a whole number.")
            num = int(num)
        out[k] = num
    if "finish_pos" not in out:
        raise ValueError("Add at least your finishing position.")
    _clean_result_extras(raw, out)
    if out.get("field_size") is not None:
        for k in ("start_pos", "finish_pos", "pos_after_lap1"):
            if k in out and out[k] > out["field_size"]:
                raise ValueError(f"{(labels.get(k) or RESULT_EXTRA_LABELS[k])} can't be more than the number of cars.")
    notes = raw.get("notes")
    if notes not in (None, ""):
        if not isinstance(notes, str):
            raise ValueError("Notes must be text.")
        if len(notes) > 500:
            raise ValueError("Notes can be up to 500 characters.")
        out["notes"] = notes.strip()
    return out


# 2.4 race-result fields (§2.1 table): the same rules as results.validate
RESULT_EXTRA_INTS = {"pos_after_lap1": (1, 200), "pit_stops": (0, 50), "penalties": (0, 50)}
RESULT_EXTRA_LABELS = {"pos_after_lap1": "Position after lap 1", "pit_stops": "Pit stops",
                       "penalties": "Penalties", "finish_status": "How the race ended",
                       "incident_laps": "Incidents by lap"}
FINISH_STATUS = ("running", "dnf", "dq", "disconnected")
INCIDENT_KINDS = ("off", "spin", "contact", "other")
INCIDENT_POINTS = (0, 1, 2, 4)
MAX_INCIDENT_LAPS = 30


def _whole(v, lo, hi, label) -> int:
    """A whole number in [lo, hi] from a number or a numeric string. Raises ValueError (plain words)."""
    if isinstance(v, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        num = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a number.")
    if not math.isfinite(num):
        raise ValueError(f"{label} must be a number.")
    if num != int(num):
        raise ValueError(f"{label} must be a whole number.")
    if not lo <= num <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return int(num)


def _clean_result_extras(raw: dict, out: dict) -> None:
    """Position after lap 1, pit stops, penalties, how it ended and incidents by lap (all optional)."""
    for k, (lo, hi) in RESULT_EXTRA_INTS.items():
        v = raw.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        out[k] = _whole(v, lo, hi, RESULT_EXTRA_LABELS[k])
    fs = raw.get("finish_status")
    fs = fs.strip().lower() if isinstance(fs, str) else fs
    if fs not in (None, ""):
        if fs not in FINISH_STATUS:
            raise ValueError("Pick how the race ended.")
        out["finish_status"] = fs
    il = raw.get("incident_laps")
    if il in (None, []):
        return
    if not isinstance(il, list):
        raise ValueError("Incidents by lap must be a list.")
    if len(il) > MAX_INCIDENT_LAPS:
        raise ValueError(f"Incidents by lap can list up to {MAX_INCIDENT_LAPS} incidents.")
    rows = []
    for i, e in enumerate(il, 1):
        if not isinstance(e, dict):
            raise ValueError(f"Incident {i}: add the lap, what happened and the points.")
        lap = _whole(e.get("lap"), 1, 2000, f"Incident {i}: the lap")
        kind = e.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else kind
        if kind not in INCIDENT_KINDS:
            raise ValueError(f"Incident {i}: pick off track, spin, contact or other.")
        pts = e.get("points", 0)
        if pts is None or (isinstance(pts, str) and not pts.strip()):
            pts = 0
        if isinstance(pts, bool) or not isinstance(pts, (int, float, str)):
            raise ValueError(f"Incident {i}: points must be 0, 1, 2 or 4.")
        try:
            pts_n = float(pts)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Incident {i}: points must be 0, 1, 2 or 4.")
        if pts_n not in INCIDENT_POINTS:
            raise ValueError(f"Incident {i}: points must be 0, 1, 2 or 4.")
        rows.append({"lap": lap, "kind": kind, "points": int(pts_n)})
    rows.sort(key=lambda r: r["lap"])
    out["incident_laps"] = rows


def clean_review_params(raw) -> dict:
    """Optional body of POST api/review: a synced session to review. Empty = your newest race."""
    if not isinstance(raw, dict) or not raw.get("event"):
        return {}
    ev = raw.get("event")
    if not isinstance(ev, str) or not re.match(r"^[A-Za-z0-9_-]{1,80}$", ev):
        raise ValueError("That session wasn't found.")
    st = raw.get("session_type")
    if st not in (1, 2, 3) or isinstance(st, bool):
        raise ValueError("That session wasn't found.")
    return {"event": ev, "session_type": st, "track_id": _pos_int(raw.get("track_id"), "track"),
            "car_id": _pos_int(raw.get("car_id"), "car")}


def clean_goal(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Bad request.")
    out = {}
    for k in ("track", "car"):
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip() or len(v) > 200:
            raise ValueError("Pick a track and car for the goal.")
        out[k] = v.strip()
    t = raw.get("target_s")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        raise ValueError("Enter the target lap time, like 1:23.500.")
    try:
        t = float(t)
    except OverflowError:  # a huge whole number
        raise ValueError("That target lap time doesn't look right.")
    if not math.isfinite(t) or not 10 <= t <= 3600:
        raise ValueError("That target lap time doesn't look right.")
    out["target_s"] = round(float(t), 3)
    note = raw.get("note")
    if note not in (None, ""):
        if not isinstance(note, str) or len(note) > 300:
            raise ValueError("Goal notes can be up to 300 characters.")
        out["note"] = note.strip()
    gid = raw.get("id")
    if gid not in (None, ""):
        if not isinstance(gid, str) or not GOAL_ID_OK.match(gid):
            raise ValueError("Bad request.")
        out["id"] = gid
    # optional Garage 61 ids, so the goal matches its races even if a track or car is renamed
    for k, what in (("track_id", "track"), ("car_id", "car")):
        if raw.get(k) not in (None, ""):
            out[k] = _pos_int(raw.get(k), what)
    return out


def _pos_int(v, what):
    if isinstance(v, bool) or (isinstance(v, float) and not math.isfinite(v)):
        raise ValueError(f"Pick a {what}.")
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Pick a {what}.")
    if not 0 < n < 10_000_000 or (isinstance(v, float) and v != n):
        raise ValueError(f"Pick a {what}.")
    return n


SETTINGS_WRITE_ERROR = "Debrief couldn't save your settings. Is %LOCALAPPDATA%\\Debrief read-only or full?"
NO_TOKEN_SAVED = "Save your Garage 61 token first."
FINISH_SETUP = "Finish setup first: paste your Garage 61 token on the welcome screen."


def friendly(e: BaseException) -> str:
    if isinstance(e, Garage61Error):
        m = str(e)
        if m.startswith("No Garage 61 token"):
            return "No Garage 61 token saved yet. Open Settings and paste your token."
        m = m.replace("Check settings.toml, or create", "Paste it again in Settings, or create")
        m = m.replace("Open settings.toml and paste", "Open Settings and paste")
        return m if m.startswith("Garage 61") else f"Garage 61: {m}"
    if isinstance(e, SystemExit):
        m = str(e.code) if e.code not in (None, 0) else "Stopped."
        if "settings.toml" in m or "Setup.bat" in m:   # app.load_settings speaks to the from-source setup
            app.say(f"Settings problem: {m}")
            if "typo" in m:
                return ("Debrief couldn't read your saved settings (details in debrief.log). "
                        "Open Settings and save them again.")
            return "Debrief's settings are missing. Close Debrief and open it again to see the welcome screen."
        return m
    return f"Something unexpected went wrong. Details were saved to debrief.log in {app.data_folder_label()}."


def design_tokens() -> str:
    """The colour/font tokens from the review templates, so the window always matches them."""
    try:
        html = (Path(app.__file__).resolve().parent / "templates" / "library.html").read_text(encoding="utf-8")
        css = html.split("<style>", 1)[1]
        cut = css.index("*{box-sizing")
        tokens = css[:cut]
        if ":root" in tokens:
            return tokens
    except (OSError, IndexError, ValueError):
        pass
    return FALLBACK_TOKENS


# ---------------------------------------------------------------- the sample review (2.8)
# A made-up race, drawn up here and never saved in the library, so someone waiting for their token can see
# what a review looks like. It goes through the real analysis and the real page, so it always matches.
SAMPLE_LABEL = ('<div id="debrief-sample-label" role="note" style="background:var(--warn);color:var(--panel);'
                'font:600 14px/1.4 var(--body);padding:10px 16px;text-align:center">Sample review. '
                "This is a made-up race on a made-up track, so you can see what a review looks like. "
                "None of it is your driving.</div>")
_SAMPLE_CORNERS = ((150, 18.0), (420, 24.0), (700, 15.0), (1000, 28.0), (1250, 20.0), (1550, 17.0),
                   (1850, 26.0), (2150, 14.0), (2420, 22.0))
_SAMPLE_LOCK = threading.Lock()
_SAMPLE_PAGE = []


def _sample_lap(np, rng, slow_at=None):
    """One made-up lap of a 2.6 km circuit: Garage 61's telemetry columns and the lap time."""
    length, vmax, width, hz = 2600.0, 55.0, 110.0, 30
    grid = np.linspace(0, length, int(length) + 1)
    speed = np.full_like(grid, vmax)
    for at, apex in _SAMPLE_CORNERS:
        low = apex + rng.normal(0, 0.6)
        off = np.abs(grid - at)
        off = np.minimum(off, length - off)
        speed = np.minimum(speed, low + (vmax - low) * np.clip(off / width, 0, 1) ** 1.3)
    if slow_at is not None:
        speed[np.abs(grid - slow_at) < 80] *= 0.8
    rows = ["Speed,LapDistPct,Brake,Throttle,Gear,ABSActive,LatAccel,LongAccel,SteeringWheelAngle,RPM,Lat,Lon"]
    t, d, before = 0.0, 0.0, float(speed[-2])
    while d < length:
        v = float(np.interp(d, grid, speed))
        acc = (v - before) * hz
        brake = min(1.0, max(0.0, -acc / 12.0))
        throttle = 1.0 if acc >= -0.5 and brake < 0.05 else 0.0
        rows.append(f"{v:.3f},{d / length:.6f},{brake:.3f},{throttle},{3 if v < 35 else 5},False,0,{acc:.3f},0,6000,"
                    f"{35.0 + d * 1e-6:.7f},-80.0")
        before = v
        d += v / hz
        t += 1 / hz
    return "\n".join(rows) + "\n", t


def sample_page() -> bytes:
    """The sample review's page, made once per run (a second or two the first time)."""
    with _SAMPLE_LOCK:
        if _SAMPLE_PAGE:
            return _SAMPLE_PAGE[0]
        import tempfile
        import numpy as np
        from .. import engine, report
        rng = np.random.default_rng(61)
        laps, tel = [], {}
        for i in range(1, 13):
            text, t = _sample_lap(np, rng, slow_at=1250 if i == 7 else None)
            lid = f"sample{i}"
            laps.append({"id": lid, "lapTime": round(t, 3), "lapNumber": i, "clean": i != 3,
                         "_start": 1000.0 + i * (t + 2), "startTime": "2026-01-01T18:00:00Z"})
            tel[lid] = text
        meta = {"driver": "Sample Driver", "event_id": "sample", "track": "Sample Street Circuit",
                "car": "Sample Car", "started_at": "2026-01-01T18:00:00Z", "session": "Race", "multiclass": False,
                "salutation": "", "generated_at": "2026-01-01T19:00:00+00:00"}
        data = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
        with tempfile.TemporaryDirectory() as td:
            html = Path(report.write(Path(td), data)).read_bytes()
        m = re.search(rb"<body\b[^>]*>", html)
        at = m.end() if m else 0
        _SAMPLE_PAGE.append(html[:at] + SAMPLE_LABEL.encode("utf-8") + html[at:])
        return _SAMPLE_PAGE[0]


# ---------------------------------------------------------------- background jobs
JOB_TITLES = {"review": "Reviewing your last race", "check": "Checking the connection",
              "prep": "Building your track prep page", "team": "Updating the team hub",
              "sync": "Syncing your Garage 61 history", "schedule": "Loading the iRacing season schedule",
              "scan": "Scanning the replay", "rival": "Reading a rival's lap", "update": "Updating Debrief", "weekly": "Updating this week's team leaderboards",
              "setuplab": "Reading your telemetry files"}
BACKGROUND_TITLES = {"refresh": "Updating your saved reviews to the new page layout"}


class Jobs:
    """One background job at a time: a review, a connection check, a prep page, a team refresh or a
    history sync. A sync gives way: starting anything else pauses it (it resumes on the next sync)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.st = {"state": "idle"}
        self.stop_sync = threading.Event()
        self.stop_scan = threading.Event()
        self.thread = None
        self.on_update_started = None   # set by the server: close Debrief so the installer can replace it
        self.closing = False            # set while Debrief closes for an update: no new jobs
        self.background = {}            # name -> thread: quiet housekeeping that writes the library
        self.refresh_fn = None          # the saved-review refresh: pauses for jobs, restarts after them
        self.refresh_pause = None
        self.bg_progress = {}           # name -> {"done": k, "total": n} (the refresh reports it per review)

    def set_background_progress(self, name: str, done: int, total: int) -> None:
        with self.lock:
            self.bg_progress[name] = {"done": int(done), "total": int(total)}

    def run_background(self, name: str, fn) -> bool:
        """Run housekeeping (the saved-review refresh) on a tracked thread: the watchdog, the update and
        any new job wait for it, so it's never cut off mid-write or racing a review in the same folder."""
        if name not in BACKGROUND_TITLES:
            raise ValueError(name)
        with self.lock:
            if self.closing or (name in self.background and self.background[name].is_alive()):
                return False
            t = threading.Thread(target=fn, daemon=True, name="bg-" + name)
            self.background[name] = t
            t.start()
        return True

    def _background_alive(self):
        with self.lock:
            return [t for t in self.background.values() if t.is_alive()]

    def background_running(self, name: str) -> bool:
        with self.lock:
            t = self.background.get(name)
            return bool(t is not None and t.is_alive())

    def wait_background(self, timeout: float) -> bool:
        """Wait for housekeeping to finish. True if none is running now."""
        end = time.monotonic() + timeout
        for t in self._background_alive():
            t.join(max(0.0, end - time.monotonic()))
        return not self._background_alive()

    @property
    def busy(self) -> bool:
        """A job or housekeeping is running: Debrief must not stop now."""
        return self.running or bool(self._background_alive())

    @property
    def kind(self):
        with self.lock:
            return self.st.get("kind") if self.st.get("state") == "running" else None

    def pause_sync(self, wait_s: float = 20.0) -> bool:
        """Ask a running sync to stop at its next checkpoint and wait for it. True if nothing is running now."""
        t = None
        with self.lock:
            if self.st.get("state") != "running":
                return True
            if self.st.get("kind") != "sync":
                return False
            self.stop_sync.set()
            self.stop_scan.set()
            t = self.thread
        if t is not None:
            t.join(wait_s)
        return not self.running

    def snapshot(self):
        with self.lock:
            out = json.loads(json.dumps(self.st))
            bg = [k for k, t in self.background.items() if t.is_alive()]
            prog = {k: dict(v) for k, v in self.bg_progress.items() if k in bg}
        if bg:
            out["background"] = bg
        if prog:
            out["background_progress"] = prog
        return out

    @property
    def running(self):
        with self.lock:
            return self.st.get("state") == "running"

    def start(self, kind: str, params: dict | None = None) -> dict:
        if kind not in JOB_TITLES:
            raise ValueError(kind)
        if self.closing:
            return {"ok": False, "error": "Debrief is closing to install an update."}
        if kind != "sync" and self.kind == "sync":
            self.pause_sync()
        # the saved-review refresh steps aside: it stops after the review it's writing (well under a second)
        if self.refresh_pause is not None:
            self.refresh_pause.set()
        if not self.wait_background(30.0):
            return {"ok": False, "error": "Debrief is still updating your saved reviews to the new page layout. "
                                          "Try again in a moment."}
        with self.lock:
            if self.closing:
                return {"ok": False, "error": "Debrief is closing to install an update."}
            if self.st.get("state") == "running":
                return {"ok": False, "error": "Debrief is already busy with: " + self.st.get("title", "a job") + "."}
            self.seq += 1
            title = JOB_TITLES[kind]
            if kind == "review" and (params or {}).get("event"):
                title = "Reviewing your " + {1: "practice session", 2: "qualifying session"}.get((params or {}).get("session_type"), "race")
            self.st = {"state": "running", "kind": kind, "id": self.seq, "title": title, "frac": 0.0,
                       "message": "Starting...", "steps": [], "started": time.time()}
            jid = self.seq
            if kind == "sync":
                self.stop_sync.clear()
            if kind in ("scan", "rival", "setuplab"):
                self.stop_scan.clear()
            self.thread = threading.Thread(target=self._run, args=(kind, jid, dict(params or {})), daemon=True,
                                           name=f"job-{kind}")
            self.thread.start()
        return {"ok": True, "id": jid}

    def _progress(self, jid):
        def cb(frac, msg):
            with self.lock:
                if self.st.get("id") != jid:
                    return
                if frac is not None:
                    self.st["frac"] = max(self.st["frac"], min(1.0, float(frac)))
                msg = str(msg)
                self.st["message"] = msg.strip()
                # per-lap telemetry ticks update the message only; real steps go in the list
                if not msg.startswith(("Telemetry for lap ", "Telemetry for session ", "Scanning replay: ", "Reading the lap: ",
                                       "Reading telemetry file ")):
                    self.st["steps"].append(msg.strip())
                    del self.st["steps"][:-60]
        return cb

    def _run(self, kind, jid, params=None):
        params = params or {}
        cb = self._progress(jid)
        result, error = None, None
        app.say(f"--- {datetime.now().isoformat(timespec='seconds')} debrief desktop {kind}")
        try:
            cfg = app.load_settings()
            if kind == "review":
                page = app.cmd_review(cfg, params.get("event"), open_after=False, progress=cb,
                                      session_kind=params.get("session_type"), track_id=params.get("track_id"),
                                      car_id=params.get("car_id"))
                result = {"review": Path(page).parent.name}
            elif kind == "scan":
                from .. import replay_scan
                try:
                    api = app.make_api(cfg)
                except (SystemExit, Exception):  # noqa: BLE001  (a scan never needs Garage 61)
                    api = None
                result = replay_scan.for_review(cfg["library_folder"], params["review_path"], api=api, progress=cb,
                                                should_stop=self.stop_scan.is_set)
            elif kind == "rival":
                from .. import rival_read
                try:
                    api = app.make_api(cfg)
                except (SystemExit, Exception):  # noqa: BLE001  (reading a lap never needs Garage 61)
                    api = None
                result = rival_read.read_for_review(cfg["library_folder"], params["review_path"], params["car"], api=api, progress=cb,
                                                    should_stop=self.stop_scan.is_set)
            elif kind == "setuplab":
                result = _run_setuplab(cfg, cb, self.stop_scan.is_set)
            elif kind == "sync":
                result = {"sync": app.cmd_sync(cfg, progress=cb, should_stop=self.stop_sync.is_set)}
            elif kind == "prep":
                page = app.cmd_prep(cfg, params["track_id"], params["car_id"], progress=cb)
                result = {"page": _lib_rel(cfg, page)}
            elif kind == "update":
                result = {"update": _run_update(cb)}
                if self.on_update_started:
                    self.on_update_started()
            elif kind == "schedule":
                result = {"schedule": _run_schedule(params, cb)}
            elif kind == "team":
                page = app.cmd_team(cfg, progress=cb)
                result = {"page": _lib_rel(cfg, page)}
            elif kind == "weekly":
                res = app.cmd_weekly(cfg, progress=cb, series_ids=params.get("series_ids"),
                                     force_old=bool(params.get("force_old")))
                result = {"page": _lib_rel(cfg, res["page"]), "requests": int(res.get("requests") or 0)}
            else:
                lines = app.cmd_check(cfg, progress=cb)
                result = {"lines": lines}
                if hasattr(lines, "rows"):   # 2.6: [{"text", "level": "info"|"ok"}] for the window's check list
                    try:
                        result["rows"] = [r for r in lines.rows() if isinstance(r, dict)]
                    except Exception:  # noqa: BLE001  (the plain lines still show)
                        app.say("ERROR reading the connection check rows:\n" + traceback.format_exc())
        except Garage61Error as e:
            app.say(f"ERROR Garage 61: {e}")
            error = friendly(e)
        except SystemExit as e:
            app.say(f"STOPPED: {e.code}")
            error = friendly(e)
        except Exception as e:  # noqa: BLE001
            app.say("ERROR (unexpected):\n" + traceback.format_exc())
            error = friendly(e)
        with self.lock:
            if self.st.get("id") != jid:
                return
            self.st["state"] = "error" if error else "done"
            self.st["error"] = error
            self.st["result"] = result
            self.st["frac"] = self.st["frac"] if error else 1.0
            self.st["finished"] = time.time()
        self._resume_refresh()

    def _resume_refresh(self):
        """After a job, carry on with the saved-review refresh if it was paused before finishing."""
        if self.refresh_fn is None or self.refresh_pause is None or self.closing:
            return
        self.refresh_pause.clear()
        try:
            self.run_background("refresh", self.refresh_fn)
        except Exception:  # noqa: BLE001
            pass


def _run_update(cb) -> dict:
    """Download the newest release, check its SHA-256 and start its installer silently."""
    from .. import updater
    if not app.FROZEN:
        raise SystemExit("This copy of Debrief runs from source, so it updates with git, not the installer.")
    try:
        cb(0.03, "Checking for the newest version...")
        info = updater.check(force=True)
        if not info["newer"]:
            raise SystemExit(f"You already have the newest version ({info['current']}).")
        path = updater.download(info, progress=cb)
        # 2.7.0: say what was checked. "Signature verified" only once a release key is set and the .sig
        # really verified; until then the installer's SHA-256 matched the published checksum, no more.
        cb(0.97, updater.installing_message())
        updater.launch(path, info.get("latest"))
    except updater.UpdateError as e:
        raise SystemExit(str(e))
    return {"version": info["latest"]}


def _run_schedule(params, cb) -> dict:
    """Import a season PDF: one you picked (params["path"], a temp copy) or iRacing's current one."""
    from .. import schedule
    import tempfile
    path = params.get("path")
    tmp_dir = None
    try:
        if not path:
            cb(0.1, "Downloading the season schedule from iRacing...")
            tmp_dir = tempfile.mkdtemp(prefix="debrief-sched-")   # ours, so it's removed even if the download fails
            path = schedule.download_official(dest_dir=tmp_dir)
        cb(0.4, "Reading the schedule (this takes a few seconds)...")
        try:
            info = schedule.import_pdf(path, kind="pdf" if params.get("path") else "official")
        except schedule.ScheduleError as e:
            raise SystemExit(str(e))
        cb(0.95, f"Loaded {info['season']}: {info['series']} series, {info['weeks']} weeks.")
        return info
    except schedule.ScheduleError as e:
        raise SystemExit(str(e))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if params.get("path"):
            try:
                Path(params["path"]).unlink()
                Path(params["path"]).parent.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------- Setup Lab and the live coach (2.12)
SETUPLAB_CACHE = "setuplab-index.json"     # in the cache folder (DEBRIEF_CACHE_DIR in tests)
NO_TELEMETRY = ("Debrief can't find your telemetry folder ({folder}). If iRacing saves telemetry somewhere else, "
                "set the folder in Settings > Setup Lab and Coach.")
IDENT_OK = re.compile(r"^[^\x00-\x1f<>\"|?*]{1,160}$")     # an iRacing car path or track code
MAX_SERIES_CHOICES = 500


def setuplab_cache_path(cfg) -> Path:
    return Path(cfg["cache_dir"]) / SETUPLAB_CACHE


def _run_setuplab(cfg, cb, should_stop) -> dict:
    """Read every telemetry file once (setuplab.index): the first time all of them, then only new ones. Read
    only: nothing in iRacing's folders is ever written."""
    from .. import setuplab
    folder = settings_io.telemetry_folder(cfg)
    if not folder.is_dir():
        raise SystemExit(NO_TELEMETRY.format(folder=folder))
    cb(0.02, "Looking for telemetry files...")

    def prog(done, total, name):
        cb(0.02 + 0.96 * done / max(1, total), f"Reading telemetry file {done} of {total}")
    idx = setuplab.index(folder, cache_path=setuplab_cache_path(cfg), progress=prog, stop=should_stop)
    n, combos = len(idx["files"]), setuplab.combos(idx)
    if idx["stopped"]:
        cb(None, f"Stopped. {idx['read']} file(s) read this time; they are kept and the rest are read next time.")
    else:
        ct = "1 car and track" if len(combos) == 1 else f"{len(combos)} cars and tracks"
        cb(1.0, f"{n} telemetry file{'s' if n != 1 else ''} for {ct} ({idx['read']} new).")
    return {"files": n, "read": idx["read"], "cached": idx["cached"], "bad": len(idx["bad"]),
            "stopped": bool(idx["stopped"]), "combos": len(combos)}


class SetupLabIndex:
    """Setup Lab's saved index (setuplab.index's cache file) in memory, read again only when the file changes, so
    the combos list and a report never scan the telemetry folder."""

    def __init__(self):
        self.lock = threading.Lock()
        self.key, self.idx, self.keys = None, None, frozenset()

    def load(self, cache_path: Path):
        """(index dict shaped like setuplab.index()'s, the set of cached file keys), or (None, empty) before the
        first read."""
        from .. import setuplab
        try:
            st = Path(cache_path).stat()
        except OSError:
            return None, frozenset()
        key = (str(cache_path), st.st_mtime_ns, st.st_size)
        with self.lock:
            if key == self.key:
                return self.idx, self.keys
        try:
            raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, frozenset()
        if not isinstance(raw, dict) or raw.get("version") != setuplab.CACHE_VERSION or not isinstance(raw.get("files"), dict):
            return None, frozenset()
        files, bad = [], []
        for rec in raw["files"].values():
            if not isinstance(rec, dict) or not isinstance(rec.get("file"), str):
                continue
            if rec.get("ok"):
                files.append(rec)
            else:
                bad.append({"file": rec["file"], "error": str(rec.get("error") or "unreadable")[:200]})
        files.sort(key=lambda r: (r.get("sort") or 0, r["file"]))
        idx = {"folder": None, "files": files, "read": 0, "cached": len(files), "bad": bad, "stopped": False,
               "missing_folder": False}
        keys = frozenset(raw["files"])
        with self.lock:
            self.key, self.idx, self.keys = key, idx, keys
        return idx, keys


def unread_files(folder: Path, keys) -> int | None:
    """Telemetry files in the folder that the saved index doesn't have yet (new or changed). None if unreadable."""
    n = 0
    try:
        for p in folder.iterdir():
            if p.suffix.lower() != ".ibt":
                continue
            st = p.stat()
            if f"{p.name}|{st.st_size}|{st.st_mtime_ns}" not in keys:
                n += 1
    except OSError:
        return None
    return n


def _words(s) -> list:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", s)


def _split_track(name) -> tuple:
    """'Circuit Zolder (Grand Prix)' or 'Zolder Circuit - Grand Prix' -> ('Circuit Zolder', 'Grand Prix')."""
    full = re.sub(r"\s+", " ", str(name or "")).strip()
    m = re.match(r"^(.*?)\s*(?:\((.*)\)|\s[-–]\s(.*))\s*$", full)
    return (m.group(1), m.group(2) or m.group(3) or "") if m else (full, "")


_CONFIG_SKIP = {"pits", "layout", "configuration", "config", "circuit"}


def _config_words(cfg_text) -> set:
    w = [x for x in _words(cfg_text) if x not in _CONFIG_SKIP and not re.fullmatch(r"(19|20)\d\d", x)]
    out = set()
    for x in w:
        out.update(("grand", "prix") if x == "gp" else (x,))
    return out


def track_names_match(a, b) -> bool:
    """Same track and layout, whoever named it: Garage 61 ('Circuit Zolder (Grand Prix)'), iRacing's files
    ('Zolder Circuit', 'Grand Prix') or the season schedule. The bases must match word for word after the filler
    is gone (one may be the other's short form, 'Spa' / 'Spa-Francorchamps'); two named layouts must share a word."""
    (ba, ca), (bb, cb) = _split_track(a), _split_track(b)
    wa, wb = set(_words(library.short_track_name(ba))), set(_words(library.short_track_name(bb)))
    if not wa or not wb or not (wa <= wb or wb <= wa):
        return False
    xa, xb = _config_words(ca), _config_words(cb)
    return not (xa and xb and not (xa & xb))


def car_names_match(a, b, path=None) -> bool:
    """Same car: 'McLaren 570S GT4' (Garage 61, the schedule) and iRacing's own name or car path ('mclaren570sgt4').
    Garage 61's car and track ids are its own, not iRacing's, so names are all there is to go on."""
    ca, cb = "".join(_words(a)), "".join(_words(b))
    if ca and ca == cb:
        return True
    cp = "".join(_words(path))
    if cp and ca and cp == ca:
        return True
    wa, wb = set(_words(a)), set(_words(b))
    return len(wa) >= 2 and len(wb) >= 2 and (wa <= wb or wb <= wa)


def ibt_track_label(track: dict) -> str:
    t = track or {}
    name, cfg = str(t.get("name") or t.get("code") or ""), str(t.get("config") or "")
    return f"{name} ({cfg})" if cfg else name


def match_setuplab_combo(combos: list, car, track):
    """The Setup Lab car + track for a Prepare view's car and track, or None."""
    for c in combos or []:
        if car_names_match(car, c.get("car"), c.get("car_path")) and track_names_match(track, c.get("track")):
            return c
    return None


def series_key(car_path: str, track_code: str) -> str:
    return f"{str(car_path).lower()}|{str(track_code).lower()}"


def schedule_series_setup(combo: dict | None, series_id=None) -> tuple:
    """(open | fixed | None, where it came from) from the season schedule: the series picked on Prepare, else every
    series that races this car at this track this season, when they all agree."""
    try:
        from .. import schedule
        sched = schedule.load_current()
    except Exception:  # noqa: BLE001  (no schedule is fine)
        return None, ""
    if not sched:
        return None, ""
    if series_id:
        ser = schedule.find_series(sched, series_id)
        if ser and ser.get("setup") in ("open", "fixed"):
            return ser["setup"], f"the season schedule ({ser.get('name') or 'this series'})"
    if not combo:
        return None, ""
    names, kinds = [], set()
    for ser in sched.get("series") or []:
        if not isinstance(ser, dict):
            continue
        for w in ser.get("weeks") or []:
            cars = (w.get("cars") if isinstance(w, dict) else None) or ser.get("cars") or []
            if isinstance(w, dict) and track_names_match(w.get("track"), combo.get("track")) and \
                    any(car_names_match(c, combo.get("car"), combo.get("car_path")) for c in cars):
                names.append(str(ser.get("name") or "a series"))
                kinds.add(ser.get("setup"))
                break
    known = kinds - {None}
    if len(known) == 1 and None not in kinds:
        k = known.pop()
        return k, "the season schedule (" + ", ".join(names[:2]) + (" and others" if len(names) > 2 else "") + ")"
    return None, ""


def _jsonable(o):
    """Plain JSON: numpy numbers become Python ones, NaN and infinity become null (the window's JSON.parse would
    refuse them)."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, (str, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "tolist"):
        return _jsonable(o.tolist())
    if hasattr(o, "item"):
        return _jsonable(o.item())
    return str(o)


COACH_FIELDS = {"in_races": bool, "markers": bool, "feedback": bool, "lap_delta": bool, "brake_beep": bool,
                "top_n": (1, 3, "Focus corners must be 1, 2 or 3."), "volume": (0, 100, "Volume must be 0 to 100."),
                "rate": (-10, 10, "Speaking speed must be -10 to 10."), **coach2.EXTRA_FIELDS}
COACH_TEST_TEXT = "Brake marker, T3"     # Coach 2.0: a phrase every voice pack says


def voice_test_close_s(v, rate) -> float:
    """How long the test voice stays open: the phrase's own length when rendered, else its spoken length at this
    speed (slow speech takes far longer), with room to spare."""
    from .. import voice as voice_mod
    dur = None
    try:
        fn = getattr(v, "duration", None)
        dur = fn(COACH_TEST_TEXT) if fn else None
    except Exception:  # noqa: BLE001
        dur = None
    if not dur:
        dur = voice_mod.speak_seconds(COACH_TEST_TEXT, rate)
    return max(4.0, 1.5 * float(dur) + 2.0)


def clean_coach_settings(raw) -> dict:
    """The coach settings the window may change. Raises ValueError (plain words)."""
    if not isinstance(raw, dict) or not raw or len(raw) > 20:
        raise ValueError("Bad request.")
    out = {}
    for k, v in raw.items():
        rule = COACH_FIELDS.get(k)
        if rule is None:
            raise ValueError("Bad request.")
        if rule is bool:
            if not isinstance(v, bool):
                raise ValueError("Pick on or off.")
            out[k] = v
            continue
        if callable(rule):                  # Coach 2.0: text fields and the measured delay (coach2.EXTRA_FIELDS)
            out[k] = rule(v)
            continue
        lo, hi, msg = rule
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and (not math.isfinite(v) or v != int(v))):
            raise ValueError(msg)
        if not lo <= int(v) <= hi:
            raise ValueError(msg)
        out[k] = int(v)
    return out


def saved_coach_settings() -> dict:
    d = settings_io.load_extra("coach").get("settings")
    return d if isinstance(d, dict) else {}


def _rebuild_library(cfg) -> None:
    """Refresh the library page (Goals tab) after a change. A failure is logged, not shown: the page
    is rebuilt again the next time it's opened (see Handler._lib)."""
    try:
        library.rebuild(cfg["library_folder"])
    except Exception:  # noqa: BLE001
        app.say("ERROR rebuilding library:\n" + traceback.format_exc())


def _lib_rel(cfg, page) -> str:
    """Path of a page inside the library folder, as the window's lib/ URLs use it."""
    try:
        return Path(page).resolve().relative_to(Path(cfg["library_folder"]).resolve()).as_posix()
    except ValueError:
        return Path(page).name


# ---------------------------------------------------------------- review list
class ReviewIndex:
    """The window's review list. Summaries come from the library's row index (library.review_summaries), so
    only reviews that changed since the last look are read."""
    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def _summary(p: Path) -> dict:
        d = json.loads(p.read_text(encoding="utf-8"))
        s = library._summary_from(d, p)
        if s is None:
            raise ValueError("not a review")
        return ReviewIndex._format(dict(s, dir=p.parent.name))

    @staticmethod
    def _format(s: dict) -> dict:
        if s.get("n_laps") is None:
            raise ValueError("no laps list")
        best = s.get("best_t")
        return {"dir": s["dir"], "track": s.get("track") or "Unknown track", "car": s.get("car") or "Unknown car",
                "started_at": s.get("started_at") or "",
                "date": library.local_day(s.get("started_at")) or s["dir"][:10],
                "session": s.get("session") or "", "best": app.fmt_t(best) if best else "",
                "laps": s["n_laps"]}

    def list(self, folder: Path) -> list:
        items = []
        with self.lock:
            for s in library.review_summaries(folder):
                try:
                    items.append(self._format(s))
                except Exception:  # noqa: BLE001  (an old or odd review: skip it)
                    continue
        items.sort(key=lambda r: (r["started_at"], r["dir"]), reverse=True)
        groups, order = {}, []
        for r in items:
            k = (r["track"], r["car"])
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(r)
        from ..library import short_track_name
        return [{"track": short_track_name(k[0]), "track_full": k[0], "car": k[1], "reviews": groups[k]} for k in order]


class RowsCache:
    """library.list_reviews rows, worked out again only when a review.json or result.json changes (the Skills
    bar asks on every open). library.scan_key only stats the files."""
    def __init__(self):
        self.key, self.val = None, []
        self.lock = threading.Lock()

    def rows(self, folder: Path) -> list:
        try:
            key = library.scan_key(folder)
        except OSError:
            key = None
        with self.lock:
            if key is not None and key == self.key:
                return self.val
        rows = library.list_reviews(folder)
        with self.lock:
            self.key, self.val = key, rows
        return rows


# ---------------------------------------------------------------- Track Intel uploads (2.4)
UPLOAD_ID_OK = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
INTEL_FIELDS = ("track", "car", "lap1", "hotspots", "passing", "limits", "focus", "car_note", "ignore")
INTEL_MODES = ("fill_empty", "append", "replace")


class IntelUploads:
    """Parsed Track Intel sheets, kept in memory only (never on disk) under a random id: at most MAX at a
    time (the oldest goes first), each one forgotten after TTL seconds or once it has been imported."""
    MAX = 3
    TTL = 30 * 60

    def __init__(self):
        self.lock = threading.Lock()
        # one sheet is parsed at a time (2.4.0 review H3): each parse is bounded by xlsx.MAX_CELLS/MAX_TEXT,
        # and this keeps two uploads at once from doubling the peak
        self.parse_lock = threading.Lock()
        self.items = {}   # id -> {"parsed", "name", "t"}
        self.clock = time.monotonic   # replaced in tests

    def _expire(self):
        now = self.clock()
        for k in [k for k, v in self.items.items() if now - v["t"] > self.TTL]:
            del self.items[k]

    def add(self, parsed, name) -> str:
        uid = secrets.token_urlsafe(12)
        with self.lock:
            self._expire()
            while len(self.items) >= self.MAX:
                del self.items[min(self.items, key=lambda k: self.items[k]["t"])]
            self.items[uid] = {"parsed": parsed, "name": name, "t": self.clock()}
        return uid

    def get(self, uid):
        if not isinstance(uid, str) or not UPLOAD_ID_OK.match(uid):
            raise ValueError("That upload has expired. Pick the file again.")
        with self.lock:
            self._expire()
            it = self.items.get(uid)
        if it is None:
            raise ValueError("That upload has expired. Pick the file again.")
        return it

    def drop(self, uid):
        with self.lock:
            self.items.pop(uid, None)


def clean_intel_request(body, uploads: IntelUploads):
    """(upload, sheet index, {col: field}, mode) from an intel-preview / intel-import body."""
    if not isinstance(body, dict):
        raise ValueError("Bad request.")
    it = uploads.get(body.get("upload_id"))
    sheets = (it["parsed"] or {}).get("sheets") or []
    sh = body.get("sheet", 0)
    if isinstance(sh, bool) or not isinstance(sh, int) or not 0 <= sh < len(sheets):
        raise ValueError("Pick a sheet from the list.")
    ncols = len((sheets[sh] or {}).get("header") or [])
    raw = body.get("mapping")
    if not isinstance(raw, dict) or not raw or len(raw) > 200:
        raise ValueError("Pick what each column holds.")
    mapping = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k.isdigit() or len(k) > 3 or not 0 <= int(k) < max(ncols, 1):
            raise ValueError("That column isn't in the sheet. Upload the file again.")
        if v not in INTEL_FIELDS:
            raise ValueError("Pick what each column holds.")
        if v != "ignore":
            mapping[int(k)] = v
    if list(mapping.values()).count("track") != 1:
        raise ValueError("Pick the one column that holds the track name.")
    mode = body.get("mode", "fill_empty")
    if mode not in INTEL_MODES:
        raise ValueError("Pick how to combine the sheet with your track cards.")
    return it, sh, mapping, mode


def _num_in(v, lo, hi, msg):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError(msg)
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(msg)
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(msg)
    return x


def _short_text(v, what, n=200):
    if not isinstance(v, str) or not v.strip() or len(v) > n or re.search(r"[\x00-\x1f<>]", v):
        raise ValueError(f"Pick a {what}.")
    return v.strip()


# ---------------------------------------------------------------- Discord (2.5)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NONCE_OK = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
MAX_SHARE_SIDE = 2400


def png_size(data: bytes):
    """(width, height) of a PNG from its IHDR, or None if it isn't a PNG Discord would take from Debrief."""
    if len(data) < 33 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR" or data[8:12] != b"\x00\x00\x00\r":
        return None
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (0 < w <= MAX_SHARE_SIDE and 0 < h <= MAX_SHARE_SIDE):
        return None
    return w, h


class DiscordStage:
    """Share cards waiting for the Post click: in memory only (never a file), under a single-use random nonce.
    At most MAX at a time (the oldest goes first); each one is forgotten after TTL seconds. Staging never
    touches the network: only api/discord-post sends anything, and only with a nonce it takes from here."""
    MAX = 3
    TTL = 10 * 60

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}               # nonce -> {"png", "created", "sha256", "width", "height"}
        self.clock = time.monotonic   # replaced in tests

    def _expire(self):
        now = self.clock()
        for k in [k for k, v in self.items.items() if now - v["created"] > self.TTL]:
            del self.items[k]

    def add(self, png: bytes, width: int, height: int) -> str:
        nonce = secrets.token_urlsafe(16)
        with self.lock:
            self._expire()
            while len(self.items) >= self.MAX:
                del self.items[min(self.items, key=lambda k: self.items[k]["created"])]
            self.items[nonce] = {"png": png, "created": self.clock(), "sha256": hashlib.sha256(png).hexdigest(),
                                 "width": width, "height": height}
        return nonce

    def pop(self, nonce):
        """The staged card for nonce, removed so it can never be used again; None if unknown or expired."""
        if not isinstance(nonce, str) or not NONCE_OK.match(nonce):
            return None
        with self.lock:
            self._expire()
            return self.items.pop(nonce, None)

    def __len__(self):
        with self.lock:
            self._expire()
            return len(self.items)


def redact_webhook(text: str, url: str | None) -> str:
    """text with the webhook address and its token taken out (for a message or a log line)."""
    text = str(text)
    if url:
        text = text.replace(url, "…")
        tok = url.rstrip("/").rsplit("/", 1)[-1]
        if len(tok) >= 20:
            text = text.replace(tok, "…")
    return re.sub(r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+", r"\1…", text)


# ---------------------------------------------------------------- Practice: session review and corner coach (2.13)
PRACTICE_CACHE = "practice"         # folder in the cache folder: one JSON file per worked-out result
PRACTICE_VERSION = 2                # bump when a result's shape changes, so old cache files are not used
PRACTICE_MEM = 48                   # results kept in memory
PRACTICE_DISK = 300                 # result files kept in the cache folder (the oldest go first)
CORNER_FILES = 10                   # the corner coach reads at most this many recent files of one car + track ...
CORNER_DAYS = 30                    # ... from at most this many days before the newest of them
SESSION_ID_OK = re.compile(r"^[^\x00-\x1f<>\"]{1,400}$")


def practice_versions() -> list:
    """Debrief's version and the review modules' own version numbers: part of every cached result's name, so a
    new Debrief (or a changed module) never shows a result worked out by an older one (2.13 review)."""
    from .. import __version__
    out = [__version__, PRACTICE_VERSION]
    for name in ("sessionibt", "cornercoach", "setuplab", "engine"):
        mod = app.optional_module(name)
        out.append([name, getattr(mod, "VERSION", None)])
    return out


def practice_key(kind: str, paths, extra=None) -> str:
    """A name for one result: what it is plus each file's name, size and change time, so a changed or new file
    makes a new result, and Debrief's and the modules' versions (practice_versions). Raises OSError when a file
    is gone."""
    files = []
    for p in paths:
        st = Path(p).stat()
        files.append([Path(p).name, st.st_size, st.st_mtime_ns])
    raw = json.dumps([practice_versions(), kind, sorted(files), extra], sort_keys=True, default=str)
    return kind + "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


class PracticeStore:
    """Session reviews and corner-coach reports, worked out once per set of files and kept in memory and in the
    cache folder, so opening a session again is instant. Two requests for the same result wait for one another
    instead of working it out twice. Only Debrief's own cache files are ever written or removed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mem = {}                   # key -> value (insertion order = age)
        self.busy = {}                  # key -> lock while it is being worked out
        self.made = 0                   # how many results were worked out (not read back): for tests
        self.list_key, self.list_val = None, None

    def get(self, cache_dir, key: str, make):
        with self.lock:
            if key in self.mem:
                v = self.mem.pop(key)
                self.mem[key] = v
                return v
            klock = self.busy.setdefault(key, threading.Lock())
        with klock:
            with self.lock:
                if key in self.mem:
                    return self.mem[key]
            folder = Path(cache_dir) / PRACTICE_CACHE
            path = folder / (key + ".json")
            val = None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("key") == key:
                    val = raw.get("value")
            except (OSError, ValueError):
                val = None
            if val is None:
                val = _jsonable(make())
                with self.lock:
                    self.made += 1
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"key": key, "value": val}, allow_nan=False), encoding="utf-8")
                    os.replace(tmp, path)
                    self._prune(folder)
                except (OSError, ValueError) as e:
                    app.say(f"Practice: couldn't save a result to the cache ({type(e).__name__}: {e}).")
            with self.lock:
                self.mem[key] = val
                while len(self.mem) > PRACTICE_MEM:
                    self.mem.pop(next(iter(self.mem)))
                self.busy.pop(key, None)
        return val

    @staticmethod
    def _prune(folder: Path):
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        for p in files[:max(0, len(files) - PRACTICE_DISK)]:
            try:
                p.unlink()
            except OSError:
                pass


def _same_combo(s: dict, car_path, track_code) -> bool:
    return (str(s.get("car_path") or "").lower() == str(car_path or "").lower()
            and str(s.get("track_code") or "").lower() == str(track_code or "").lower())


def corner_paths(sessions: list, car_path, track_code, upto=None) -> list:
    """The telemetry files the corner coach reads for one car + track: the newest CORNER_FILES files (up to the
    session that starts at `upto`, when given) from no more than CORNER_DAYS days before the newest. Oldest first."""
    mine = [s for s in sessions if _same_combo(s, car_path, track_code)
            and (upto is None or float(s.get("sort") or 0) <= float(upto) + 1e-6)]
    mine.sort(key=lambda s: -float(s.get("sort") or 0))
    out, newest = [], None
    for s in mine:
        srt = float(s.get("sort") or 0)
        newest = srt if newest is None else newest
        if newest - srt > CORNER_DAYS * 86400:
            break
        for p in reversed(s.get("paths") or []):
            if len(out) >= CORNER_FILES:
                break
            out.append(p)
        if len(out) >= CORNER_FILES:
            break
    return list(reversed(out))


def corner_report(paths: list, sessions_used: int = 0) -> dict:
    """The corner coach over these files, ready for the window and the live coach."""
    from .. import cornercoach
    if not paths:
        return {"ok": False, "message": "No telemetry files for this car and track yet.", "cards": [], "unclear": [],
                "fine": [], "summary": None, "laps": 0, "files": 0, "phrases": {}, "sessions": 0}
    rep = cornercoach.analyze_files([str(p) for p in paths])
    rep.pop("params", None)
    rep["files"] = len(paths)
    rep["sessions"] = sessions_used
    rep["phrases"] = cornercoach.voice_phrases(rep, top_n=3)
    return rep


def weak_from_cards(rep: dict) -> list:
    """Corner-coach cards as the live coach's focus corners: biggest loss first, each with its short phrase."""
    out = []
    for c in (rep or {}).get("cards") or []:
        if not c.get("voice"):
            continue
        out.append({"id": c.get("id"), "apex_m": c.get("apex_m"), "lost_s": c.get("lost_s"), "phrase": c.get("voice"),
                    "rule": c.get("rule"), "source": "corner coach"})
    return out


# ---------------------------------------------------------------- Practice loop (2.14): one thing this session
PLAN_DIR = "practice"               # folder next to settings.toml: one plans file per car + track


def plans_folder() -> Path:
    """Where the practice plans are kept: <settings folder>/practice (practiceplan.plan_path names the files)."""
    return app.settings_path().parent / PLAN_DIR


def plan_brief(p: dict) -> dict:
    """A saved plan as the window's history list needs it (no per-lap rows)."""
    sc = p.get("score") if isinstance(p.get("score"), dict) else None
    out = {k: p.get(k) for k in ("plan_id", "made", "kind", "corner", "id", "metric", "text", "confidence",
                                 "experiment", "repeat")}
    if sc:
        out["score"] = {k: sc.get(k) for k in ("text", "hits", "measured", "hit_share", "verdict", "verdict_text",
                                               "session", "session_date", "scored")}
        out["score"]["next"] = (sc.get("next") or {}).get("action")
    else:
        out["score"] = None
    return out


def plan_view(p: dict | None) -> dict | None:
    """A plan for the window: everything but the score's per-lap rows are kept small (the last 30)."""
    if not isinstance(p, dict):
        return None
    out = dict(p)
    sc = out.get("score")
    if isinstance(sc, dict):
        out["score"] = dict(sc, per_lap=(sc.get("per_lap") or [])[-30:])
    return out


def _mark_voice_tested():
    """First-run walkthrough: the voice has been heard (Test voice spoke). Never raises."""
    try:
        fr = app.optional_module("firstrun")
        if fr is not None:
            fr.mark("voice_tested")
    except Exception:  # noqa: BLE001
        pass


def wheel_saved() -> dict | None:
    """The wheel button saved for mute (coach.json "wheel"), checked; None when there is none."""
    from .. import wheelmute
    return wheelmute.clean_binding(settings_io.load_extra("coach").get("wheel"))


# ---------------------------------------------------------------- server
class DebriefServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, heartbeat_timeout=20.0, startup_grace=90.0):
        super().__init__(("127.0.0.1", 0), Handler)
        self.secret = secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self.jobs = Jobs()
        self.reviews = ReviewIndex()
        self.profile_cache = RowsCache()
        self.intel = IntelUploads()
        self.discord_stage = DiscordStage()
        self.discord_info = None      # {"hint", "name", "channel_id"} from the last Check (memory only)
        self.timeout_s = heartbeat_timeout
        self.last_ping = time.monotonic() + max(0.0, startup_grace - heartbeat_timeout)
        self.stopping = threading.Event()
        self.jobs.on_update_started = self._close_for_update
        self.update_outcome = None   # how the last in-app update went (set at start; see updater.pending_result)
        self.exit = os._exit         # replaced in tests
        # 2.12 Setup Lab: the saved telemetry index, kept in memory
        self.setuplab = SetupLabIndex()
        # 2.13 Practice: session reviews and corner-coach reports, worked out once per set of files
        self.practice = PracticeStore()
        # 2.12 live coach: one runner, never a Jobs job (it runs for an hour and must not block reviews)
        self.coach_lock = threading.Lock()
        self.coach = None             # livecoach.LiveRunner while started
        self.coach_voice = None
        self.coach_hotkey = None
        self.coach_last = None        # the last status of a stopped runner (what it said stays on the page)
        self.coach_starting = False   # a start is under way (building the runner can take a moment)
        self.coach_gen = 0            # bumped by stop_coach
        self.coach_weak_info = None   # {"races": n} for the car + track iRacing reports
        self.test_voice = None
        self.test_gen = 0             # one Test voice at a time; a newer one (or Stop) wins
        self.test_state = None        # how the last Test voice went, for the window
        # tests only: callables that replace live iRacing and the Windows voice (e.g. an .ibt playback and FakeVoice)
        self.coach_source_factory = None   # () -> source
        self.coach_voice_factory = None    # (livecoach.Settings) -> voice
        # 2.14 Practice loop: plans are read and written under this lock (one file per car + track)
        self.plan_lock = threading.Lock()
        self.coach_focus_info = None       # {"plan_id", "corner", "text"} of the plan the running coach watches
        # coach.json (the coach's settings and the wheel button): one read-change-write at a time
        self.coach_json_lock = threading.Lock()
        # 2.14 wheel button mute: one poller, started with the coach (or for Learn), stopped with it
        self.wheel = None                  # wheelmute.WheelMute
        self.wheel_note = ""               # how the last Learn went, in plain words
        self.wheel_backend_factory = None  # tests only: () -> a stand-in joystick backend

    # errors a request thread didn't catch itself (a crash inside do_GET/do_POST before any reply). The
    # library's default prints them to stderr, which is /dev/null under pythonw: they vanished (2.7.0 M6).
    # The window's own disconnects (it closed, or Edge dropped a kept-alive socket) are routine, not errors.
    QUIET_ERRORS = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self.QUIET_ERRORS) or isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054):
            return
        try:
            app.say("ERROR handling a request from the window:\n" + traceback.format_exc())
        except Exception:  # noqa: BLE001  (logging must never take the server down)
            pass

    def _close_for_update(self):
        """The installer is running: stop in a few seconds (after the window has seen the result) so it
        can replace Debrief's files. It waits for Debrief to close, installs and reopens Debrief.
        No new job starts meanwhile, and anything still writing (a job or the review refresh) finishes first.
        Reading telemetry for Setup Lab is only a cache, so it is told to stop now (what it has read is kept)."""
        self.jobs.closing = True
        with self.jobs.lock:
            if self.jobs.st.get("state") == "running" and self.jobs.st.get("kind") == "setuplab":
                self.jobs.stop_scan.set()

        def stop():
            end = time.monotonic() + 60
            while time.monotonic() < end:
                with self.jobs.lock:   # the update job itself is finishing: only another writer counts
                    other = self.jobs.st.get("state") == "running" and self.jobs.st.get("kind") != "update"
                if not other and not self.jobs._background_alive():
                    break
                time.sleep(0.2)
            app.say("Closing for the update.")
            self.stopping.set()
            self.shutdown()
            self.exit(0)
        threading.Timer(4.0, stop).start()

    # ---------- the live coach (2.12)
    def shutdown(self):
        """Stops the coach (its thread, the mute key and the voice) first, whoever closes Debrief: the window going
        away, an update or Ctrl+C. No new coach starts after this."""
        self.stopping.set()
        try:
            self.stop_coach()
        except Exception:  # noqa: BLE001
            app.say("ERROR stopping the coach:\n" + traceback.format_exc())
        coach2.on_shutdown(self)
        super().shutdown()

    def server_close(self):
        try:
            self.stop_coach()
        except Exception:  # noqa: BLE001
            pass
        super().server_close()

    def coach_settings(self):
        from .. import livecoach
        return livecoach.Settings.from_dict(saved_coach_settings())

    def coach_weak_for(self, car: dict, track: dict) -> list:
        """The weak corners for the car + track iRacing reports. 2.13: the corner coach's cards for that car and
        track first (each with its short phrase, said after the corner); else prep.weak_corners on your saved race
        reviews of that car at that track, matched by name (Garage 61's ids aren't iRacing's). None found: an empty
        list, and the coach cues from your best lap only."""
        try:
            weak, laps = self.corner_weak_for(car, track)
        except Exception:  # noqa: BLE001  (the race reviews still work)
            app.say("ERROR reading the corner coach for the live coach:\n" + traceback.format_exc())
            weak, laps = [], 0
        if weak:
            self.coach_weak_info = {"races": 0, "source": "corner coach", "laps": laps}
            return weak
        self.coach_weak_info = {"races": 0}
        prep = app.optional_module("prep")
        try:
            cfg = app.load_settings()
        except SystemExit:
            return []
        if prep is None:
            return []
        rows = library.race_rows(self.profile_cache.rows(Path(cfg["library_folder"])))
        tname, car = ibt_track_label(track), car or {}
        mine = [r for r in rows if car_names_match(r.get("car"), car.get("name"), car.get("path"))
                and track_names_match(r.get("track"), tname)]
        self.coach_weak_info = {"races": len(mine)}
        return prep.weak_corners(mine) if mine else []

    # ---------- Practice (2.13): telemetry sessions, the session review and the corner coach
    def practice_sessions(self, cfg) -> tuple:
        """(sessions newest first, with each file's full path; Setup Lab's index or None). Never opens more than
        each file's header once (sessionibt remembers SubSessionIDs); the list is kept until the index changes."""
        si = app.optional_module("sessionibt")
        if si is None:
            raise ValueError("Practice isn't available right now. Details were saved to debrief.log in "
                             + app.data_folder_label() + ".")
        idx, _keys = self.setuplab.load(setuplab_cache_path(cfg))
        if idx is None:
            return [], None
        folder = settings_io.telemetry_folder(cfg)
        with self.setuplab.lock:
            lk = (self.setuplab.key, str(folder))
        with self.practice.lock:
            if self.practice.list_key == lk:
                return self.practice.list_val, idx
        ss = si.sessions(dict(idx, folder=str(folder)))
        with self.practice.lock:
            self.practice.list_key, self.practice.list_val = lk, ss
        return ss, idx

    def corner_coach_for(self, cfg, sessions: list, car_path, track_code, upto=None) -> dict:
        """The corner coach for one car + track over its recent files (corner_paths), cached per set of files."""
        paths = corner_paths(sessions, car_path, track_code, upto)
        used = sum(1 for s in sessions if set(s.get("paths") or []) & set(paths))
        try:
            key = practice_key("corners", paths)
        except OSError:
            return {"ok": False, "message": "Some telemetry files have gone. Press Read my telemetry again.",
                    "cards": [], "unclear": [], "fine": [], "summary": None, "laps": 0, "files": 0, "phrases": {}}
        return self.practice.get(cfg["cache_dir"], key, lambda: corner_report(paths, used))

    def corner_weak_for(self, car: dict, track: dict) -> tuple:
        """(the corner coach's cards as focus corners, clean laps they come from) for the car + track iRacing
        reports, or ([], 0)."""
        cp, tc = (car or {}).get("path"), (track or {}).get("code")
        if not cp or not tc:
            return [], 0
        try:
            cfg = app.load_settings()
        except SystemExit:
            return [], 0
        ss, idx = self.practice_sessions(cfg)
        if not idx or not any(_same_combo(s, cp, tc) for s in ss):
            return [], 0
        rep = self.corner_coach_for(cfg, ss, cp, tc)
        return weak_from_cards(rep), int(rep.get("laps") or 0)

    # ---------- Practice loop (2.14): one thing this session, kept per car + track
    def combo_sessions(self, cfg, car, track) -> tuple:
        """(this car + track's sessions newest first, every session, Setup Lab's index or None)."""
        ss, idx = self.practice_sessions(cfg)
        return [x for x in ss if _same_combo(x, car, track)], ss, idx

    def new_plan(self, cfg, car, track, ss=None, plans=None, avoid=None) -> dict:
        """A fresh plan from the corner coach over this car + track's latest files, saved as the current one.
        Call with plan_lock held."""
        pp = app.optional_module("practiceplan")
        if ss is None:
            ss, _idx = self.practice_sessions(cfg)
        # only sessions whose files are still there (a file moved away since the last read doesn't spoil the plan)
        ss = [x for x in ss if all(Path(f).exists() for f in (x.get("paths") or []))]
        mine = [x for x in ss if _same_combo(x, car, track)]
        rep = self.corner_coach_for(cfg, ss, car, track) if mine else corner_report([])
        hist = plans if plans is not None else pp.load_plans(plans_folder(), car, track)
        plan = pp.make_plan(rep, history=hist, car=car, track=track, avoid=avoid)
        plan.update({"car_path": car, "track_code": track,
                     "car_name": mine[0].get("car") if mine else car, "track_name": mine[0].get("track") if mine else track,
                     "after_sort": float(mine[0].get("sort") or 0) if mine else 0.0,
                     "after_date": mine[0].get("date") if mine else None, "made_ts": time.time()})
        plan = _jsonable(plan)
        if not pp.save_plan(plans_folder(), car, track, plan):
            app.say(f"Practice plan: couldn't save the plan for {car} at {track}.")
        self._refresh_coach_focus()
        return plan

    def _refresh_coach_focus(self):
        """A new current plan: a running coach picks it up (on its own thread, before its next sample) instead of
        keeping the plan it started with."""
        with self.coach_lock:
            r = self.coach
        if r is not None:
            try:
                r.refresh_focus()
            except Exception:  # noqa: BLE001
                pass

    def current_plan(self, cfg, car, track, fresh=False) -> tuple:
        """(the current plan for this car + track, every saved plan newest first). A new plan is made when there is
        none, when fresh is asked for, or when the current one has been scored (the score says keep or move on;
        make_plan reads that from the history)."""
        pp = app.optional_module("practiceplan")
        with self.plan_lock:
            plans = pp.load_plans(plans_folder(), car, track)
            cur = plans[0] if plans else None
            if fresh or cur is None or isinstance(cur.get("score"), dict):
                avoid = []
                if fresh:                   # Pick another: pass over the plans he skipped since the last score
                    for p in plans:
                        if isinstance(p.get("score"), dict):
                            break
                        avoid.append(p.get("id"))
                cur = self.new_plan(cfg, car, track, plans=plans, avoid=avoid)
                plans = pp.load_plans(plans_folder(), car, track)
            return cur, plans

    def score_session(self, cfg, me: dict, ss: list, auto=False) -> dict | None:
        """Score this car + track's current plan on one session (me: a sessions-ibt row with its paths), save the
        score, and make the next plan. auto: only when the plan is unscored and the session is newer than it (else
        None). Raises ValueError (plain words) when an asked-for score can't be done."""
        pp = app.optional_module("practiceplan")
        if pp is None:
            raise ValueError("The practice plan isn't available right now.")
        car, track = me.get("car_path"), me.get("track_code")
        with self.plan_lock:
            plans = pp.load_plans(plans_folder(), car, track)
            cur = plans[0] if plans else None
            if cur is None or isinstance(cur.get("score"), dict):
                if auto:
                    return None
                raise ValueError("There's no plan waiting to be scored for this car and track. Make one on the "
                                 "Practice page first.")
            if float(me.get("sort") or 0) <= float(cur.get("after_sort") or 0) + 1e-6:
                if auto:
                    return None
                raise ValueError("This session was driven before the plan was made. Drive a session with the plan, "
                                 "then score that one.")
            rep = self.corner_coach_for(cfg, ss, car, track)
            sc = pp.score_files(cur, list(me.get("paths") or []), report=rep)
            focus = cur.get("kind") in ("focus", "consistency")
            if auto and not (sc.get("measured") if focus else sc.get("laps")):
                return None                 # no clean lap through the focus corner in this one: wait for the next
            sc.pop("next_plan", None)
            sc.update({"session": me.get("id"), "session_date": me.get("date"), "auto": bool(auto)})
            sc = _jsonable(sc)
            if not pp.save_score(plans_folder(), car, track, cur.get("plan_id"), sc):
                app.say(f"Practice plan: couldn't save the score for {car} at {track}.")
                raise ValueError("The score couldn't be saved. Details were saved to debrief.log.")
            plans = pp.load_plans(plans_folder(), car, track)
            nxt = self.new_plan(cfg, car, track, ss=ss, plans=plans)
            app.say(f"Practice plan scored on a session of {car} at {track}: {sc.get('text')}")
        return {"plan": plan_view(dict(cur, score=sc)), "score": sc, "next_plan": plan_view(nxt)}

    def session_score(self, car, track, sid) -> dict | None:
        """The saved plan scored on this session (with its score), or None."""
        pp = app.optional_module("practiceplan")
        if pp is None:
            return None
        for p in pp.load_plans(plans_folder(), car, track):
            sc = p.get("score")
            if isinstance(sc, dict) and sc.get("session") == sid:
                return plan_view(p)
        return None

    def coach_focus_for(self, car: dict, track: dict, length_m=None):
        """For the live coach (on its own thread, whenever the car + track changes): the current practice plan for
        that car + track as a live focus, or None. Never makes a new plan (that reads telemetry files)."""
        self.coach_focus_info = None
        pp = app.optional_module("practiceplan")
        cp, tc = (car or {}).get("path"), (track or {}).get("code")
        if pp is None or not cp or not tc:
            return None
        plans = pp.load_plans(plans_folder(), cp, tc)
        cur = plans[0] if plans else None
        if not cur or isinstance(cur.get("score"), dict) or cur.get("kind") not in ("focus", "consistency"):
            return None
        f = pp.LiveFocus(cur, length_m=length_m)
        if not f.active:
            return None
        self.coach_focus_info = {"plan_id": cur.get("plan_id"), "corner": cur.get("corner"), "text": cur.get("text")}
        return f

    # ---------- wheel button mute (2.14)
    def _wheel(self):
        from .. import wheelmute
        with self.coach_lock:
            if self.wheel is None:
                be = self.wheel_backend_factory() if self.wheel_backend_factory else None
                self.wheel = wheelmute.WheelMute(on_press=self._wheel_press, binding=wheel_saved(), backend=be,
                                                 log=app.say)
            return self.wheel

    def _wheel_press(self):
        with self.coach_lock:
            r = self.coach
        if r is not None and r.running():
            r.toggle()

    def _wheel_learned(self, binding):
        """Learn mode ended (on the poller's thread): save the button, or say none was pressed."""
        if binding:
            try:
                with self.coach_json_lock:
                    data = settings_io.load_extra("coach")
                    data["wheel"] = binding
                    settings_io.save_extra("coach", data)
                self.wheel_note = f"Saved: {binding.get('label')}."
                app.say("Wheel mute button saved.")
            except OSError as e:
                app.say(f"ERROR saving the wheel button: {type(e).__name__}: {e}")
                self.wheel_note = "The button couldn't be saved. Details were saved to debrief.log."
        else:
            self.wheel_note = "No button was pressed. Try again and press the button within 15 seconds."
        with self.coach_lock:               # decided under the lock a starting coach takes, so a coach starting
            w = self.wheel                  # right now keeps its wheel button
            run = self.coach_starting or (self.coach is not None and self.coach.running())
            if w is not None and not run:
                w.stop(0)                   # only listened for Learn: the coach isn't on (no wait: this thread)

    def wheel_learn(self) -> dict:
        w = self._wheel()
        if not w.available():
            return {"ok": False, "error": "Wheel buttons can't be read on this computer."}
        self.wheel_note = ""
        if not w.learn(on_learned=self._wheel_learned):
            return {"ok": False, "error": "Wheel buttons can't be read on this computer."}
        return {"ok": True}

    def wheel_clear(self) -> dict:
        try:
            with self.coach_json_lock:
                data = settings_io.load_extra("coach")
                data.pop("wheel", None)
                settings_io.save_extra("coach", data)
        except OSError as e:
            app.say(f"ERROR clearing the wheel button: {type(e).__name__}: {e}")
            raise ValueError(SETTINGS_WRITE_ERROR)
        with self.coach_lock:
            w = self.wheel
        if w is not None:
            w.cancel_learn()
            w.set_binding(None)
        self.wheel_note = "Wheel button cleared."
        return {"ok": True}

    def wheel_status(self) -> dict:
        b = wheel_saved()
        with self.coach_lock:
            w = self.wheel
        st = w.status() if w is not None else {"state": "off", "text": "", "binding": None}
        state = st.get("state")
        if state in ("off", "not_set") or (state == "ready" and not b):
            text = (f"{b['label']}. It mutes the coach while the coach is on." if b else "No wheel button chosen yet.")
            state = "saved" if b else "not_set"
        else:
            text = st.get("text") or ""
        return {"state": state, "text": text, "binding": b, "label": b["label"] if b else None,
                "note": self.wheel_note}

    def _start_wheel(self):
        try:
            b = wheel_saved()
            if b is None:
                return
            w = self._wheel()
            with self.coach_lock:           # the same lock _wheel_learned decides under
                w.set_binding(b)
                w.start()
        except Exception:  # noqa: BLE001  (the coach works without it)
            app.say("ERROR starting the wheel mute button:\n" + traceback.format_exc())

    def _stop_wheel(self):
        with self.coach_lock:
            w = self.wheel
        if w is not None:
            try:
                w.stop()
            except Exception:  # noqa: BLE001
                pass

    def start_coach(self, source_factory=None, mode: str = "live", log_header=None) -> dict:
        """Start listening to iRacing and coaching. Never raises: a problem is a message, and the log has the rest.
        Two clicks (or two windows) at once start one coach: a start already under way answers "already", and a
        coach built while Debrief began closing, or after Stop was pressed, is stopped again at once."""
        if self.jobs.closing or self.stopping.is_set():
            return {"ok": False, "error": "Debrief is closing."}
        from .. import livecoach
        with self.coach_lock:
            if self.coach is not None and self.coach.running():
                return {"ok": True, "already": True}
            if self.coach_starting:
                return {"ok": True, "already": True, "starting": True}
            self.coach_starting = True
            gen = self.coach_gen
            old = (self.coach, self.coach_hotkey, self.coach_voice)
            self.coach = self.coach_hotkey = self.coach_voice = None
        try:
            return self._start_coach(livecoach, old, gen, source_factory, mode, log_header)
        finally:
            with self.coach_lock:
                self.coach_starting = False

    def _start_coach(self, livecoach, old, gen, source_factory=None, mode="live", log_header=None) -> dict:
        self._close_coach_parts(*old)
        coach2.close_side(self)               # Coach 2.0: a Preview / Measure voice gives the device back
        settings = self.coach_settings()
        settings.muted = False
        r = vc = None
        try:
            src = (source_factory() if source_factory else self.coach_source_factory() if self.coach_source_factory
                   else livecoach.IrsdkSource(rate_hz=settings.sample_hz))
            vc = coach2.make_voice(self, settings)
            folder, index_of = None, None
            try:
                cfg = app.load_settings()
                tf = settings_io.telemetry_folder(cfg)
                folder = tf if tf.is_dir() else None
                cache = setuplab_cache_path(cfg)
                index_of = lambda: self.setuplab.load(cache)[0]       # noqa: E731  (Setup Lab's saved index)
            except (SystemExit, OSError, KeyError):
                pass
            self.coach_weak_info = None
            given = carsetup.coach_reference(self, settings)     # 2.15: a teammate's lap, when he chose it
            r = livecoach.LiveRunner(src, vc, settings=settings, telemetry_dir=folder, weak_for=self.coach_weak_for,
                                     log=app.say, reference_index=index_of, focus_for=self.coach_focus_for,
                                     reference=given, keep_given=given is not None, log_dir=coach2.logs_dir(),
                                     mode=mode, log_header=log_header)
            r.start()
        except Exception:  # noqa: BLE001
            app.say("ERROR starting the coach:\n" + traceback.format_exc())
            self._close_coach_parts(r, None, vc)
            return {"ok": False, "error": "The coach couldn't start. Details were saved to debrief.log in "
                                          + app.data_folder_label() + "."}
        with self.coach_lock:
            closing = self.stopping.is_set() or self.jobs.closing
            clash = closing or gen != self.coach_gen or (self.coach is not None and self.coach is not r)
            if not clash:
                self.coach, self.coach_voice = r, vc
        if clash:
            self._close_coach_parts(r, None, vc)
            app.say("Coach not started: " + ("Debrief is closing." if closing else "stopped while it was starting."))
            return {"ok": False, "error": "Debrief is closing." if closing else "The coach was stopped while it was starting."}
        hk = None
        try:   # Windows only; elsewhere, or if another program has the key, the mute button still works
            hk = coach2.hotkeys(self, settings, r)      # mute, repeat (2.17), quiet for a minute (2.17)
            hk.start(timeout=2.0)
        except Exception:  # noqa: BLE001
            app.say("ERROR starting the coach's mute key:\n" + traceback.format_exc())
        with self.coach_lock:
            if self.coach is r:
                self.coach_hotkey = hk
                hk = None
        if hk is not None:      # stopped while the key was starting
            self._close_coach_parts(None, hk, None)
        self._start_wheel()
        coach2.start_wheel(self)              # Coach 2.0: the wheel button for "Repeat that"
        app.say("Coach started." if mode == "live" else "Coach rehearsal started.")
        return {"ok": True}

    @staticmethod
    def _close_coach_parts(runner, hotkey, voice, timeout=3.0) -> bool:
        done = True
        if hotkey is not None:
            try:
                hotkey.stop()
            except Exception:  # noqa: BLE001
                pass
        if runner is not None:
            try:
                done = runner.stop(timeout)
            except Exception:  # noqa: BLE001
                done = False
        if voice is not None:
            try:
                voice.close()
            except Exception:  # noqa: BLE001
                pass
        return done

    def stop_coach(self, timeout: float = 3.0) -> bool:
        """Stop the coach and wait for its thread. True when nothing of it is still running."""
        with self.coach_lock:
            r, hk, vc = self.coach, self.coach_hotkey, self.coach_voice
            self.coach = self.coach_hotkey = self.coach_voice = None
            self.coach_gen += 1           # a start still under way stops the coach it builds
            self.test_gen += 1            # ...and a test voice still starting is closed when it's ready
            if self.test_state and self.test_state.get("state") == "starting":
                self.test_state = None
            tv, self.test_voice = self.test_voice, None
        done = self._close_coach_parts(r, hk, None, timeout)
        self._stop_wheel()
        coach2.on_coach_stop(self)
        if r is not None:
            try:
                self.coach_last = r.status()
            except Exception:  # noqa: BLE001
                pass
            app.say("Coach stopped." if done else "Coach stopped (its thread is still finishing).")
        self._close_coach_parts(None, None, vc)
        self._close_coach_parts(None, None, tv)
        return done

    def coach_status(self) -> dict:
        with self.coach_lock:
            r, hk = self.coach, self.coach_hotkey
        if r is not None:
            try:
                st = r.status()
            except Exception:  # noqa: BLE001
                app.say("ERROR reading the coach's status:\n" + traceback.format_exc())
                st = {"state": "error", "text": "The coach's status couldn't be read.", "running": r.running()}
        else:
            last = self.coach_last or {}
            st = {"state": "stopped", "text": "Coach off", "running": False, "muted": False, "quiet_reason": None,
                  "reference": last.get("reference"), "corners": last.get("corners") or 0, "weak": last.get("weak") or [],
                  "last_lap": last.get("last_lap"), "spoken": last.get("spoken") or [], "events": [], "voice": None,
                  "error": "", "errors": 0, "car": last.get("car") or "", "track": last.get("track") or "",
                  "settings": self.coach_settings().to_dict()}
        st["hotkey"] = {"key": "Ctrl+Shift+M", "on": bool(hk is not None and getattr(hk, "registered", False))}
        try:
            st["wheel"] = self.wheel_status()
        except Exception:  # noqa: BLE001
            st["wheel"] = {"state": "error", "text": "The wheel button couldn't be read.", "binding": None}
        if r is None:
            st["focus"] = (self.coach_last or {}).get("focus")
        wi = self.coach_weak_info
        note = None
        if st.get("weak") and wi is not None and wi.get("source") == "corner coach":
            n = int(wi.get("laps") or 0)
            note = (f"From the Corner coach: your last {n} clean laps of this car here. After each focus corner it "
                    "says the one thing to try there.")
        elif st.get("reference") and not st.get("weak"):
            if wi is not None and not wi.get("races"):
                note = "No saved race reviews of this car at this track, so the cues come from your best lap only."
            elif wi is not None:
                note = "Your race reviews here show no corner that keeps costing time, so the cues come from your best lap only."
        st["weak_note"] = note
        with self.coach_lock:
            st["test_voice"] = dict(self.test_state) if self.test_state else None
        carsetup.coach_extra(self, st)      # 2.15: coaching against a teammate's lap
        coach2.extra(self, st)              # Coach 2.0: keys, measure, preview, rehearsal, wheel repeat
        return _jsonable(st)

    def coach_test_voice(self) -> dict:
        """Say one cue now, at the saved volume and speed: through the running coach's voice, or a voice made for
        this on its own thread (Windows speech can take seconds to start, and a request thread must not wait for it)
        and closed once the phrase has had time to finish at the saved speed. coach_status()["test_voice"] says how
        it went: {"state": "starting" | "spoken" | "silent" | "error", "text", "reason"}."""
        with self.coach_lock:
            r, vc = self.coach, self.coach_voice
        if r is not None and r.running() and vc is not None:
            try:
                said = bool(vc.say(COACH_TEST_TEXT, priority=3))
                if said:
                    _mark_voice_tested()
                return {"ok": True, "spoken": said, "voice": _jsonable(vc.status())}
            except Exception:  # noqa: BLE001
                app.say("ERROR testing the voice:\n" + traceback.format_exc())
                return {"ok": False, "error": "The voice didn't work. Details were saved to debrief.log."}
        settings = self.coach_settings()
        with self.coach_lock:
            self.test_gen += 1
            gen = self.test_gen
            self.test_state = {"state": "starting", "text": "Speaking test phrase", "reason": "", "id": gen}
        threading.Thread(target=self._test_voice_run, args=(settings, gen), daemon=True, name="debrief-voice-test").start()
        return {"ok": True, "pending": True, "id": gen}

    def _test_voice_run(self, settings, gen):
        v = None

        def done(state, text, reason=""):
            with self.coach_lock:
                if self.test_gen == gen:
                    self.test_state = {"state": state, "text": text, "reason": reason, "id": gen}
        try:
            v = coach2.make_voice(self, settings)
            wait = getattr(v, "wait_ready", None)
            if callable(wait):
                wait(5.0)
            with self.coach_lock:
                mine = self.test_gen == gen and not self.stopping.is_set()
                if mine:
                    old, self.test_voice = self.test_voice, v
            if not mine:                    # stopped, closing, or a newer test took over
                self._close_coach_parts(None, None, v)
                return
            self._close_coach_parts(None, None, old)
            said = bool(v.say(COACH_TEST_TEXT, priority=3))
            st = v.status() or {}
            if said:
                _mark_voice_tested()
                done("spoken", "Speaking test phrase")
            else:
                done("silent", "No sound", str(st.get("reason") or "speech isn't available on this computer"))
        except Exception:  # noqa: BLE001
            app.say("ERROR testing the voice:\n" + traceback.format_exc())
            done("error", "The voice didn't work. Details were saved to debrief.log.")
            self._close_coach_parts(None, None, v)
            return

        def close_later():
            with self.coach_lock:
                mine = self.test_voice is v
                if mine:
                    self.test_voice = None
            if mine:
                self._close_coach_parts(None, None, v)
        t = threading.Timer(voice_test_close_s(v, settings.rate), close_later)
        t.daemon = True
        t.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/{self.secret}/"

    def ping(self):
        self.last_ping = max(self.last_ping, time.monotonic())

    def bye(self):
        # the page is closing (or reloading): exit in ~4 s unless a new ping arrives
        self.last_ping = time.monotonic() - self.timeout_s + 4

    WATCHDOG_RESTARTS = 5         # restarts of the loop allowed within WATCHDOG_WINDOW_S ...
    WATCHDOG_WINDOW_S = 60.0
    WATCHDOG_RESTART_SLEEP_S = 1.0

    def watchdog(self):
        """Stops Debrief once the window has gone. 2.7.0 (M6): an error inside the loop used to kill this
        thread silently and leave Debrief running with no window; now it is logged and, if the window is
        already gone, Debrief still exits. 2.7.1 (L3): with the window still open, the loop is started again
        after a short sleep (at most WATCHDOG_RESTARTS times a minute; past that Debrief stops watching and
        says so), so a passing error never leaves Debrief running after the window closes."""
        restarts = []
        while True:
            try:
                self._watchdog_loop()
                return
            except Exception:  # noqa: BLE001
                try:
                    app.say("ERROR in the window watchdog:\n" + traceback.format_exc())
                except Exception:  # noqa: BLE001
                    pass
                try:
                    idle = time.monotonic() - self.last_ping
                    gone = idle > self.timeout_s
                except Exception:  # noqa: BLE001
                    gone = True
                if gone and not self.stopping.is_set():
                    app.say("Window closed and the watchdog failed. Stopping.")
                    self.stopping.set()
                    threading.Thread(target=self.shutdown, daemon=True).start()
                    threading.Timer(5.0, self.exit, args=(0,)).start()   # even if shutdown() itself hangs
                    return
                if self.stopping.is_set():
                    return
                now = time.monotonic()
                restarts = [t for t in restarts if now - t < self.WATCHDOG_WINDOW_S] + [now]
                if len(restarts) > self.WATCHDOG_RESTARTS:
                    app.say(f"The window watchdog failed {len(restarts)} times in a minute; it has stopped. "
                            "Close Debrief from the tray or Task Manager if it stays open after the window.")
                    return
                if self.stopping.wait(self.WATCHDOG_RESTART_SLEEP_S):
                    return

    def _watchdog_loop(self):
        prev = time.monotonic()
        while not self.stopping.wait(1.0):
            now = time.monotonic()
            if now - prev > 10:  # the PC was asleep: give the window time to ping again
                self.last_ping = max(self.last_ping, now)
            prev = now
            idle = now - self.last_ping
            if idle > self.timeout_s and self.jobs.kind == "sync":
                self.jobs.pause_sync(10.0)   # a sync never keeps Debrief open: it resumes next time
            if idle > self.timeout_s and self.jobs.kind in ("scan", "rival", "setuplab"):
                self.jobs.stop_scan.set()    # a closed window never leaves the replay fast-forwarding
            if idle > self.timeout_s and not self.jobs.busy:
                app.say(f"Window closed (no heartbeat for {idle:.0f}s). Stopping.")
                self.stopping.set()
                threading.Thread(target=self.shutdown, daemon=True).start()
                return


class Handler(BaseHTTPRequestHandler):
    server: DebriefServer
    protocol_version = "HTTP/1.1"
    server_version = "Debrief"
    sys_version = ""

    timeout = 30   # a stalled client can't hold a thread forever (socket timeout on every read)

    def log_message(self, fmt, *args):  # keep the secret URL and request noise out of the log
        pass

    def parse_request(self):
        self._consumed = False   # has this request's body been read? (see _send)
        return super().parse_request()

    def _has_body(self) -> bool:
        if self.headers.get("Transfer-Encoding"):
            return True
        cl = (self.headers.get("Content-Length") or "").strip()
        return cl != "0" and cl != ""

    # ---------- helpers
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        extra = dict(extra or {})
        if not getattr(self, "_consumed", False) and self._has_body():
            # a refused request whose body we never read: close, so those bytes are never taken
            # as the next request on this connection
            extra["Connection"] = "close"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:   # the bundled fonts may be kept for a day (see _font)
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if "Content-Security-Policy" not in extra and "Cross-Origin-Resource-Policy" not in extra:
            # library pages carry their own (sandbox) policy; the fonts say cross-origin (see _font)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in extra.items():
            self.send_header(k, v)
        if extra.get("Connection", "").lower() == "close":
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _nf(self):
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _route(self):
        """Path after the secret, or None if the request isn't for this session."""
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"):
            return None
        path = urlsplit(self.path).path
        pre = "/" + self.server.secret
        if path == pre:
            return ""
        if not path.startswith(pre + "/"):
            return None
        return path[len(pre) + 1:]

    MAX_BODY = 64 * 1024

    def _body(self) -> dict:
        """The JSON body. Any problem with the body's size closes the connection after the 400 reply, so
        unread bytes are never taken as the next request."""
        cl = self.headers.get("Content-Length")
        cl = "0" if cl is None else cl.strip()
        if not cl.isdigit() or self.headers.get("Transfer-Encoding"):
            # negative, empty or junk length, or a chunked body: we can't know where the body ends
            self.close_connection = True
            raise ValueError("Bad request.")
        n = int(cl)
        if n > self.MAX_BODY:
            self.close_connection = True
            raise ValueError("Request too large.")
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:   # the client stopped sending
            self.close_connection = True
            raise ValueError("Bad request.")
        self._consumed = True
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}

    def _token_saved(self) -> bool:
        """A Garage 61 token is saved (settings readable and the token isn't empty or the example's)."""
        try:
            tok = str(app.load_settings().get("token") or "").strip()
        except SystemExit:
            return False
        return bool(tok) and not tok.startswith("PASTE")

    def _library_folder(self) -> Path | None:
        try:
            return Path(app.load_settings()["library_folder"])
        except SystemExit:
            return None

    # ---------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        r = self._route()
        if r is None:
            return self._nf()
        if r == "" and not urlsplit(self.path).path.endswith("/"):
            return self._send(302, b"", "text/plain", {"Location": "/" + self.server.secret + "/"})
        if r in ("", "index.html"):
            try:
                html = (UI / "index.html").read_bytes()
            except OSError:
                return self._nf()
            return self._send(200, html, MIME[".html"], {
                "Content-Security-Policy": ui_csp(html.decode("utf-8", "replace")),
                "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin"})
        if r.startswith("static/"):
            name = r[len("static/"):]
            if name == "tokens.css":
                return self._send(200, design_tokens().encode("utf-8"), "text/css; charset=utf-8")
            if name in ("app.css", "app.js"):
                return self._file(UI / name)
            if name == "icon.png" and ICON_PNG.exists():
                return self._file(ICON_PNG)
            if name.startswith("fonts/"):
                return self._font(name[len("fonts/"):])
            return self._nf()
        if r == "api/status":
            return self._json(self.server.jobs.snapshot())
        if r == "api/settings":
            return self._json(settings_io.public_view())
        if r == "api/reviews":
            lib = self._library_folder()
            if lib is None:
                return self._json({"groups": [], "folder": None})
            return self._json({"groups": self.server.reviews.list(lib), "folder": str(lib)})
        if r.startswith("lib/"):
            return self._lib(r[len("lib/"):])
        if r == "sample/index.html":
            return self._sample()
        if r in ("api/combos", "api/teams", "api/goals", "api/result", "api/markers", "api/pages", "api/sessions", "api/prepare", "api/prepfocus",
                 "api/notes", "api/update", "api/profile", "api/strategy", "api/weekly", "api/brand",
                 "api/diagnostics", "api/setuplab/combos", "api/setuplab/report", "api/coach/status",
                 "api/sessions-ibt", "api/session-review", "api/corner-coach", "api/plan", "api/plan/history",
                 "api/firstrun"):
            if self.headers.get("X-Debrief") != "1":
                return self._nf()
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                return getattr(self, "_get_" + r[4:].replace("/", "_").replace("-", "_"))(q)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Garage61Error as e:
                app.say(f"ERROR Garage 61: {e}")
                return self._json({"ok": False, "error": friendly(e)}, 502)
            except Exception as e:  # noqa: BLE001
                app.say("ERROR in " + r + ":\n" + traceback.format_exc())
                return self._json({"ok": False, "error": friendly(e)}, 500)
        if r in coach2.GET_ROUTES:      # Coach 2.0
            return self._carsetup_call(lambda: coach2.get(self, r, {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}), r, get=True)
        if r in carsetup.GET_ROUTES:    # 2.15 Car and setup
            return self._carsetup_call(lambda: carsetup.get(self, r, {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}), r, get=True)
        if r in racing.GET_ROUTES:      # 2.16 Racing: starts and incidents
            return self._carsetup_call(lambda: racing.get(self, r, {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}), r, get=True)
        if r in carcompare_page.GET_ROUTES:     # 2.18 Car vs car
            return self._carsetup_call(lambda: carcompare_page.get(self, r, {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}), r, get=True)
        return self._nf()

    def _carsetup_call(self, fn, r, get=False):
        """2.15: one Car and setup request, with the same error handling as the other endpoints."""
        if get and self.headers.get("X-Debrief") != "1":
            return self._nf()
        try:
            return fn()
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            app.say("ERROR in " + r + ":\n" + traceback.format_exc())
            return self._json({"ok": False, "error": friendly(e)}, 500)

    # ---------- feature helpers
    def _cfg(self):
        try:
            return app.load_settings()
        except SystemExit as e:
            raise ValueError(friendly(e))

    @staticmethod
    def _cache_api(cfg):
        """A Garage 61 client for reading its cache; None when there's no token yet. Creating it sends nothing."""
        try:
            return app.make_api(cfg)
        except Garage61Error:
            return None

    def _module(self, name):
        m = app.optional_module(name)
        if m is None:
            raise ValueError({"prep": "Track prep", "goals": "Goals", "results": "Race results", "setuplab": "Setup Lab",
                              "livecoach": "The coach", "practiceplan": "The practice plan",
                              "wheelmute": "Wheel button mute", "firstrun": "The first-run checklist"}.get(name, name) +
                             " isn't available right now. Details were saved to debrief.log in " + app.data_folder_label() + ".")
        return m

    def _fetch_allowed(self, q):
        if q.get("fetch") != "1":
            return False
        if self.server.jobs.running:
            raise ValueError("Wait for " + self.server.jobs.snapshot().get("title", "the current job").lower() +
                             " to finish first.")
        return True

    def _review_dir(self, cfg, name) -> Path:
        if not isinstance(name, str) or not REVIEW_DIR_OK.match(name) or ".." in name:
            raise ValueError("That review wasn't found.")
        d = Path(cfg["library_folder"]) / "reviews" / name
        if not (d / "review.json").is_file():
            raise ValueError("That review wasn't found.")
        return d

    # ---------- GET endpoints for the new views
    def _get_combos(self, q):
        """Combos for the Prep picker plus the track and car lists for search.
        Requests: none, unless fetch=1 and a list was never downloaded (then one request per missing list)."""
        cfg = self._cfg()
        prep = app.optional_module("prep")
        combos = []
        if prep is not None:
            combos = [dict(c) for c in prep.combos(cfg["library_folder"])]
        api = self._cache_api(cfg)
        tracks = api.cached_items("tracks") if api else None
        cars = api.cached_items("cars") if api else None
        used = 0
        if api and self._fetch_allowed(q):
            if tracks is None:
                tracks = api.tracks()
            if cars is None:
                cars = api.cars()
            used = api.requests_made
        for c in combos:
            if c.get("track_id") is None and tracks:
                c["track_id"] = app.id_for_name(tracks, c.get("track"))
            if c.get("car_id") is None and cars:
                c["car_id"] = app.id_for_name(cars, c.get("car"))
        slim = lambda items: None if items is None else sorted(
            ({"id": g["id"], "name": app.item_name(g)} for g in items if isinstance(g, dict) and isinstance(g.get("id"), int)),
            key=lambda x: x["name"].lower())
        return self._json({"ok": True, "combos": combos, "tracks": slim(tracks), "cars": slim(cars),
                           "requests": used, "prep_available": prep is not None, "token": api is not None})

    def _get_teams(self, q):
        """Your Garage 61 teams for the Settings picker. Cached list only, or 1 request with fetch=1."""
        cfg = self._cfg()
        api = self._cache_api(cfg)
        teams = api.cached_items("teams") if api else None
        used = 0
        if api and self._fetch_allowed(q):
            from .. import team as _team
            # "Check again" must see a team you've just joined
            teams = api.teams(refresh=True) if _team.can_refresh_teams(api) else api.teams()
            used = api.requests_made
        out = None if teams is None else [{"slug": str(t.get("slug")), "name": str(t.get("name") or t.get("slug"))}
                                         for t in teams if isinstance(t, dict) and t.get("slug")]
        from .. import brand
        return self._json({"ok": True, "teams": out, "team_slug": cfg["team_slug"], "requests": used,
                           "token": api is not None, "membership": brand.membership(teams, cfg["team_slug"])})

    def _get_goals(self, q):
        cfg = self._cfg()
        goals = self._module("goals")
        folder = cfg["library_folder"]
        # 2.7.1 (M6): race sessions only, as the library page's Goals tab (library.rebuild) counts them
        rows = library.race_rows(library.list_reviews(folder))
        return self._json({"ok": True, "goals": goals.load(folder), "progress": goals.progress(folder, rows=rows)})

    def _get_profile(self, q):
        """Driver profile (skill trends) summary for the window's Skills page bar. Library data only, never a
        request. The full profile is on the library page (#skills); this says whether there is one yet and
        whether the saved-review refresh is still adding skill scores to older reviews."""
        lib = self._library_folder()
        snap = self.server.jobs.snapshot()
        refresh = (snap.get("background_progress") or {}).get("refresh") if "refresh" in (snap.get("background") or []) else None
        out = {"ok": True, "available": False, "n_races": 0, "weakest": None, "refreshing": refresh}
        profile = app.optional_module("profile")
        if lib is None or profile is None:
            return self._json(out)
        rows = self.server.profile_cache.rows(lib)
        try:
            prof = profile.build(rows)
        except Exception:  # noqa: BLE001
            app.say("ERROR building the driver profile:\n" + traceback.format_exc())
            return self._json(out)
        if isinstance(prof, dict):
            out["available"] = True
            out["n_races"] = prof.get("n_races") or 0
            w = prof.get("weakest")
            out["weakest"] = w if isinstance(w, dict) else None
        return self._json(out)

    def _get_result(self, q):
        cfg = self._cfg()
        d = self._review_dir(cfg, q.get("review"))
        results = self._module("results")
        scan = None
        try:     # what a replay scan says, for the bar and the form: never official, never saved by itself
            rc = json.loads((d / "review.json").read_text(encoding="utf-8")).get("racecap") or {}
            if isinstance(rc.get("finish"), int) and (rc.get("version") or 0) >= 2:
                scan = {k: rc.get(k) for k in ("finish", "class_finish", "class_size", "cars", "official")}
        except (OSError, ValueError, AttributeError):
            pass
        return self._json({"ok": True, "review": d.name, "result": results.load(d), "scan": scan})

    SESSIONS_LIMIT = 60      # default page size of /api/sessions
    SESSIONS_MAX = 5000

    def _get_sessions(self, q):
        """Your synced sessions (races, qualifying, practice), newest first. Never a request.

        Paging: ?offset=&limit= (limit defaults to 60). Without either the whole list comes back, as before
        2.6 (a window from 2.5 shows it all); "total" is always the full count, "offset"/"limit" what this
        page used and "more" whether there are sessions after it. sessions.json is read once."""
        cfg = self._cfg()
        api = self._cache_api(cfg)
        paged = "offset" in q or "limit" in q
        offset = self._page_int(q.get("offset"), 0, "offset") if paged else 0
        limit = self._page_int(q.get("limit"), self.SESSIONS_LIMIT, "limit") if paged else None
        if limit is not None:
            limit = max(1, min(limit, self.SESSIONS_MAX))
        if api is None:
            return self._json({"ok": True, "token": False, "summary": None, "sessions": [], "total": 0,
                               "offset": offset, "limit": limit, "more": False})
        from .. import sync
        store = sync.Store.for_api(api)
        raw = sync._read_json(store.root / "sessions.json", {})
        raw = raw if isinstance(raw, dict) else {}
        all_s = [s for s in (raw.get("sessions") or []) if isinstance(s, dict)]
        summary = self._sync_summary(store, raw)
        total = len(all_s)
        page = all_s[offset:offset + limit] if limit is not None else all_s[offset:]
        # review folders end with the event's last 6 characters (+ _practice / _qualifying). Every ending a
        # session could look for ("_" + 1..6 characters + suffix) maps to the newest folder with it, so each
        # session is one dict lookup (the same answer as checking every folder name with endswith)
        by_end = {}
        for d in sorted((p.parent.name for p in (Path(cfg["library_folder"]) / "reviews").glob("*/review.json")),
                        reverse=True):
            for suf in ("", "_practice", "_qualifying"):
                if not d.endswith(suf):
                    continue
                for k in range(1, 7):
                    end = d[-(len(suf) + k + 1):]
                    if len(end) == len(suf) + k + 1 and end.startswith("_"):
                        by_end.setdefault(end, d)
        rows = []
        for s in page:
            ev = str(s.get("event") or "")
            name = None
            if ev:
                end = "_" + ev[-6:].lower() + {1: "_practice", 2: "_qualifying"}.get(s.get("type"), "")
                name = by_end.get(end)
            rows.append({k: s.get(k) for k in ("key", "event", "track_id", "car_id", "track", "car", "type", "session",
                                                 "start", "laps", "clean_laps", "best", "best_clean")} | {"review": name})
        return self._json({"ok": True, "token": True, "summary": summary, "sessions": rows,
                           "sync_on_open": cfg.get("sync_on_open", True), "total": total, "offset": offset,
                           "limit": limit, "more": offset + len(rows) < total})

    @staticmethod
    def _page_int(v, default, what) -> int:
        if v is None or v == "":
            return default
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"Bad {what}.")
        if n < 0:
            raise ValueError(f"Bad {what}.")
        return n

    @staticmethod
    def _sync_summary(store, raw: dict) -> dict:
        """store.summary(), from the sessions.json already read (same keys; one read, not two)."""
        if not hasattr(store, "state"):
            return store.summary()
        st = store.state()
        return {"synced": bool(st.get("first_complete")), "last_sync": st.get("last_success"),
                "laps": int(raw.get("laps") or 0), "sessions": len(raw.get("sessions") or []),
                "combos": len(st.get("combos") or {}), "telemetry_left": int(st.get("telemetry_left") or 0),
                "last_requests": st.get("last_requests"), "stopped": bool(st.get("stopped")),
                "warnings": list(st.get("last_warnings") or [])}

    def _get_brand(self, q):
        """Team identity for the window (Midfield Menace names and links). Constants only: no settings, no secrets."""
        from .. import brand
        return self._json({"ok": True, **brand.as_dict()})

    def _synced_sessions(self, api):
        if api is None:
            return []
        from .. import sync
        return sync.Store.for_api(api).sessions()

    def _get_prepare(self, q):
        """Everything the Prepare page shows. Never a request: schedule, sync store and library only."""
        cfg = self._cfg()
        prepare = self._module("prepare")
        api = self._cache_api(cfg)
        return self._json(prepare.view(cfg, api, self._synced_sessions(api)))

    def _get_prepfocus(self, q):
        """One series week, or any track, in one car, for the Prepare view. Never a request."""
        cfg = self._cfg()
        prepare = self._module("prepare")
        wk = q.get("week")
        if wk is not None and not (wk.isdigit() and 0 < int(wk) < 100):
            raise ValueError("Pick a week.")
        args = {k: _short_text(q[k], k) if q.get(k) else None for k in ("series", "car", "track")}
        api = self._cache_api(cfg)
        return self._json(prepare.focus(cfg, api, self._synced_sessions(api), args["series"], int(wk) if wk else None,
                                        args["car"], args["track"], q.get("plan") != "0"))

    def _get_notes(self, q):
        cfg = self._cfg()
        notes = self._module("notes")
        track = q.get("track")
        if not isinstance(track, str) or not track.strip() or len(track) > 200:
            raise ValueError("Pick a track.")
        return self._json({"ok": True, "card": notes.load(cfg["library_folder"], track), "labels": notes.LABELS,
                           "shared": self._shared_cards(cfg, notes, track), "sharing": self._sharing(cfg, notes)})

    @staticmethod
    def _sharing(cfg, notes) -> dict:
        """Team track cards: is sharing on, does the folder exist, and who you share as (cached; no request)."""
        out = {"on": bool(cfg.get("share_my_cards")), "folder_ok": False, "author": None,
               "folder": cfg.get("team_cards_folder") or None,
               "available": hasattr(notes, "shared_for") and hasattr(notes, "shared_root")}
        if not out["available"]:
            return out
        try:
            out["folder_ok"] = bool(cfg.get("team_cards_folder")) and Path(cfg["team_cards_folder"]).is_dir()
            a = notes.author_from_cache(cfg) if hasattr(notes, "author_from_cache") else None
            out["author"] = {"slug": str(a.get("slug")), "name": str(a.get("name") or a.get("slug"))[:80]} \
                if isinstance(a, dict) and a.get("slug") else None
        except Exception:  # noqa: BLE001  (a shared drive that's offline: no sharing, no error)
            app.say("ERROR reading the team cards folder:\n" + traceback.format_exc())
        return out

    @staticmethod
    def _shared_cards(cfg, notes, track) -> list:
        if not hasattr(notes, "shared_for") or not cfg.get("team_cards_folder"):
            return []
        try:
            a = notes.author_from_cache(cfg) if hasattr(notes, "author_from_cache") else None
            out = notes.shared_for(cfg, track, a.get("slug") if isinstance(a, dict) else None)
            return out if isinstance(out, list) else []
        except Exception:  # noqa: BLE001
            app.say("ERROR reading team track cards:\n" + traceback.format_exc())
            return []

    def _get_strategy(self, q):
        """Fuel planner prefill for a track and car (or a series' week). Never a request: library, sync store,
        schedule and strategy.json only."""
        cfg = self._cfg()
        strategy = self._module("strategy")
        track = _short_text(q.get("track"), "track")
        cars = [_short_text(q.get("car"), "car")] if q.get("car") else []
        from .. import schedule
        sched = schedule.load_current()
        week = None
        if q.get("series") and sched:
            ser = schedule.find_series(sched, q.get("series"))
            if ser is not None:
                cars = cars or list(ser.get("cars") or [])
                try:
                    wn = int(q.get("week") or 0)
                except ValueError:
                    raise ValueError("Pick a week.")
                week = next((w for w in ser.get("weeks") or [] if w.get("week") == wn), None)
        if not cars:
            raise ValueError("Pick a car.")
        api = self._cache_api(cfg)
        from .. import sync
        store = sync.Store.for_api(api) if api is not None else None
        pre = strategy.prefill(cfg, store, sched, track, cars, week)
        return self._json({"ok": True, "track": track, "cars": cars, "prefill": pre})

    def _get_update(self, q):
        """Is a newer Debrief out? Uses the once-a-day saved answer unless force=1. auto=1 (window opening)
        respects the check_updates setting."""
        from .. import updater
        cfg = self._cfg()
        last = self.server.update_outcome   # {"ok": False, "message": ...} when the last update didn't finish
        if q.get("auto") == "1" and not cfg.get("check_updates", True):
            return self._json({"ok": True, "skipped": True, "last_update": last})   # the outcome still shows
        try:
            info = updater.check(force=q.get("force") == "1")
        except updater.UpdateError as e:
            return self._json({"ok": False, "error": str(e), "last_update": last})
        except Exception:  # noqa: BLE001  (a broken update.json etc.: never show Python's own words)
            app.say("ERROR checking for updates:\n" + traceback.format_exc())
            return self._json({"ok": False, "error": "Couldn't check for updates right now. Try again later.",
                               "last_update": last})
        info.pop("notes", None) if q.get("auto") == "1" else None
        return self._json({"ok": True, "installed": app.FROZEN, **info, "last_update": last})

    # ---------- diagnostics (2.7.0, L3)
    DIAG_LOG_LINES = 200
    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    @staticmethod
    def scrub_diagnostics(text: str, secrets=()) -> str:
        """A log excerpt safe to paste somewhere (app.scrub_text, 2.7.1 M5): the home folder in any form becomes
        ~, the saved token and webhook (and any seen earlier, or any webhook-shaped address) become …, token /
        key / password / Authorization shapes and e-mail addresses are masked, and the bare Windows user name
        becomes [user]."""
        return redact_webhook(app.scrub_text(str(text), secrets), None)

    @staticmethod
    def _tail_lines(path: Path, n: int) -> list:
        """The last n lines of a text file, read from its end (the log can be 1 MB)."""
        try:
            size = path.stat().st_size
        except OSError:
            return []
        chunk = min(size, 256 * 1024)
        try:
            with open(path, "rb") as f:
                f.seek(size - chunk)
                data = f.read(chunk)
        except OSError:
            return []
        lines = data.decode("utf-8", "replace").splitlines()
        if chunk < size and lines:
            lines = lines[1:]   # the first line is probably cut
        return lines[-n:]

    @staticmethod
    def _dir_size(path: Path, limit_files: int = 20000):
        """Bytes under a folder, or None when it's too big to count cheaply."""
        total, n = 0, 0
        try:
            for root, _dirs, files in os.walk(path):
                for f in files:
                    n += 1
                    if n > limit_files:
                        return None
                    try:
                        total += os.stat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
        except OSError:
            return None
        return total

    def _get_diagnostics(self, q):
        """What "Copy diagnostics" in About collects: versions, what updates are checked against, how much
        is saved, where the data folder is and the end of debrief.log with the home folder, the token, the
        webhook and e-mail addresses taken out. Never a request; never a secret."""
        from .. import __version__, updater
        secrets_ = []
        cfg = None
        try:
            cfg = app.load_settings()
        except SystemExit:
            pass
        if cfg:
            secrets_.append(cfg.get("token"))
        try:
            secrets_.append(settings_io.discord_webhook())
        except Exception:  # noqa: BLE001
            pass
        try:
            level = updater.verification_level()
        except Exception:  # noqa: BLE001
            level = "unknown"
        out = {"ok": True, "version": __version__, "platform": platform.platform(aliased=True),
               "python": sys.version.split()[0], "frozen": bool(app.FROZEN), "verification_level": level,
               "settings_folder": app.scrub_home(str(app.HERE)), "reviews": None, "sessions": None,
               "cache_bytes": None, "library_folder": None, "log": []}
        if cfg:
            try:
                lib = Path(cfg["library_folder"])
                out["library_folder"] = app.scrub_home(str(lib))
                out["reviews"] = sum(1 for _ in (lib / "reviews").glob("*/review.json"))
            except OSError:
                pass
            try:
                api = self._cache_api(cfg)
                if api is not None:
                    from .. import sync
                    raw = sync._read_json(sync.Store.for_api(api).root / "sessions.json", {})
                    out["sessions"] = len((raw or {}).get("sessions") or []) if isinstance(raw, dict) else 0
            except Exception:  # noqa: BLE001
                pass
            try:
                out["cache_bytes"] = self._dir_size(Path(cfg["cache_dir"]))
            except Exception:  # noqa: BLE001
                pass
        log = Path(app.LOG_FILE) if app.LOG_FILE else app.HERE / "debrief.log"
        out["log"] = [self.scrub_diagnostics(ln, secrets_) for ln in self._tail_lines(log, self.DIAG_LOG_LINES)]
        return self._json(out)

    def _get_weekly(self, q):
        """What "Refresh this week" would do and roughly how many Garage 61 requests it would use
        (leaderboards.plan). Never a request: the schedule, your saved series, the cached lists and snapshots."""
        cfg = self._cfg()
        lb = app.optional_module("leaderboards")
        out = {"ok": True, "available": lb is not None, "schedule": False, "plan": None,
               "running": self.server.jobs.kind == "weekly"}
        if lb is None:
            return self._json(out)
        from .. import schedule, series
        sched = schedule.load_current()
        out["schedule"] = bool(sched)
        if not sched:
            return self._json(out)
        api = self._cache_api(cfg)
        plan = lb.plan(cfg, api, sched, series.load(cfg["library_folder"]))
        out["plan"] = plan if isinstance(plan, dict) else None
        out["token"] = api is not None
        return self._json(out)

    def _get_pages(self, q):
        """Pages built by the Prep and Team views, so the window can offer to reopen them."""
        cfg = self._cfg()
        lib = Path(cfg["library_folder"])
        team = lib / "team" / "index.html"
        preps = []
        for p in sorted((lib / "prep").glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            name = p.stem.replace("_", " ")
            try:  # the page's own <title> reads better than its file name
                with open(p, encoding="utf-8", errors="replace") as f:
                    m = re.search(r"<title>([^<]{1,160})</title>", f.read(4096))
                if m:
                    name = m.group(1).strip()
            except OSError:
                pass
            preps.append({"page": "prep/" + p.name, "name": name, "updated": p.stat().st_mtime})
        return self._json({"ok": True, "team": "team/index.html" if team.is_file() else None,
                           "team_updated": team.stat().st_mtime if team.is_file() else None, "prep": preps})

    def _file(self, p: Path, lib: bool = False):
        try:
            body = p.read_bytes()
        except OSError:
            return self._nf()
        ctype = MIME.get(p.suffix.lower(), "application/octet-stream")
        if lib:
            if p.suffix.lower() == ".html":
                body = with_bridge(with_fonts(body, self.server.secret))
            return self._send(200, body, ctype, {"Content-Security-Policy": LIB_CSP})
        self._send(200, body, ctype)

    def _sample(self):
        try:
            body = sample_page()
        except Exception:  # noqa: BLE001  (only a look-ahead: it must never get in the way of setup)
            app.say("ERROR making the sample review:\n" + traceback.format_exc())
            body = (b"<!doctype html><html><head><title>Sample review</title></head><body><p>The sample review "
                    b"couldn't be made on this PC. Your own reviews aren't affected.</p></body></html>")
        body = with_bridge(with_fonts(body, self.server.secret))
        return self._send(200, body, MIME[".html"], {"Content-Security-Policy": LIB_CSP})

    def _font(self, name: str):
        """static/fonts/<file>: the bundled stylesheet and woff2 files. Library pages run in a sandbox
        (an opaque origin), so for them these are cross-origin fetches: the stylesheet needs a resource
        policy that allows it and the font files (a CORS fetch, Origin: null) an allow-origin header.
        Nothing here is secret: the files are the same for every copy of Debrief."""
        if not FONT_FILE_OK.match(name):
            return self._nf()
        p = FONTS / name
        try:
            body = p.read_bytes()
        except OSError:
            return self._nf()
        self._send(200, body, MIME.get(p.suffix.lower(), "application/octet-stream"),
                   {"Access-Control-Allow-Origin": "*", "Cross-Origin-Resource-Policy": "cross-origin",
                    "Cache-Control": "private, max-age=86400"})

    def _lib(self, rel: str):
        lib = self._library_folder()
        if lib is None:
            return self._nf()
        rel = unquote(rel) or "index.html"
        if rel == "index.html":
            idx = lib / "index.html"
            newest = max((p.stat().st_mtime for p in (lib / "reviews").glob("*/review.json")), default=0)
            gf = lib / "goals.json"   # goals saved elsewhere (or a rebuild that failed) show up too
            if gf.exists():
                newest = max(newest, gf.stat().st_mtime)
            # while the saved-review refresh rewrites every review.json, each one looks newer than the page:
            # show the page as it is (refresh_all rebuilds it once at the end) unless there isn't one yet
            refreshing = self.server.jobs.background_running("refresh")
            # the page holds "the last 7 days" (the digest), so a page from an earlier day is rebuilt too
            stale_day = idx.exists() and datetime.fromtimestamp(idx.stat().st_mtime).date() != datetime.now().date()
            if not idx.exists() or ((idx.stat().st_mtime < newest or stale_day) and not refreshing):
                try:
                    library.rebuild(lib)
                except Exception:  # noqa: BLE001
                    app.say("ERROR rebuilding library:\n" + traceback.format_exc())
        root = lib.resolve()
        try:
            p = (root / rel).resolve()
            p.relative_to(root)
        except (ValueError, OSError):
            return self._nf()
        if p.suffix.lower() not in LIB_TYPES or not p.is_file() or p.name.lower() == library.INDEX_FILE.lower():
            return self._nf()
        self._file(p, lib=True)

    # ---------- POST
    def do_POST(self):
        r = self._route()
        if r is None or self.headers.get("X-Debrief") != "1":
            return self._nf()
        if r == "api/schedule-upload":
            return self._post_schedule_upload()
        if r == "api/intel-upload":
            return self._post_intel_upload()
        if r == "api/result-csv":
            return self._post_result_csv()
        if r == "api/discord-stage":
            return self._post_discord_stage()
        if r == "api/setuplab/check-file":
            return self._post_setuplab_check_file()
        if r == coach2.UPLOAD_ROUTE:        # Coach 2.0: a voice pack .zip
            return self._carsetup_call(lambda: coach2.upload(self), r)
        if r == carsetup.UPLOAD_ROUTE:      # 2.15: a teammate's .ibt
            return self._carsetup_call(lambda: carsetup.upload(self), r)
        try:  # read the body first (even for ping/bye) so a kept-alive connection stays in step
            body = self._body()
        except (ValueError, UnicodeDecodeError, RecursionError):
            extra = {"Connection": "close"} if self.close_connection else None
            return self._send(400, json.dumps({"ok": False, "error": "Bad request."}).encode("utf-8"),
                              extra=extra)
        if r == "api/ping":
            self.server.ping()
            return self._json({"ok": True})
        if r == "api/bye":
            self.server.bye()
            return self._json({"ok": True})
        if r == "api/window":
            return self._json({"ok": settings_io.save_window_size(body)})
        if r == "api/review":
            try:
                params = clean_review_params(body)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            if not self._token_saved():
                return self._json({"ok": False, "error": FINISH_SETUP, "setup": True}, 400)
            return self._json(self.server.jobs.start("review", params))
        if r == "api/replay-scan":
            try:
                d = self._review_dir(self._cfg(), body.get("review"))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json(self.server.jobs.start("scan", {"review_path": str(d)}))
        if r == "api/replay-scan-stop":
            if self.server.jobs.kind in ("scan", "rival"):
                self.server.jobs.stop_scan.set()
            return self._json({"ok": True})
        if r == "api/rival-choices":
            from .. import rival_read
            try:
                d = self._review_dir(self._cfg(), body.get("review"))
                return self._json({"ok": True, **rival_read.choices(self._cfg()["library_folder"], d)})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except SystemExit as e:
                return self._json({"ok": False, "error": str(e.code)})
        if r == "api/rival-read":
            try:
                d = self._review_dir(self._cfg(), body.get("review"))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            if not isinstance(body.get("car"), str) or not body["car"]:
                return self._json({"ok": False, "error": "Choose a driver first."}, 400)
            return self._json(self.server.jobs.start("rival", {"review_path": str(d), "car": body["car"]}))
        if r == "api/sync":
            return self._post_sync(body)
        if r == "api/sync-stop":
            if self.server.jobs.kind == "sync":
                self.server.jobs.stop_sync.set()
            return self._json({"ok": True})
        if r == "api/check":
            return self._json(self.server.jobs.start("check"))
        if r == "api/update":
            return self._json(self.server.jobs.start("update"))
        if r in ("api/prep", "api/team", "api/goals", "api/result", "api/series", "api/prepare-choice", "api/notes", "api/schedule",
                 "api/strategy", "api/racecap-link", "api/intel-preview", "api/intel-import", "api/weekly", "api/discord-post",
                 "api/discord-check", "api/setuplab/read", "api/setuplab/stop", "api/setuplab/series", "api/coach/start",
                 "api/coach/stop", "api/coach/mute", "api/coach/settings", "api/coach/test-voice", "api/plan/new",
                 "api/plan/score", "api/coach/wheel-learn", "api/coach/wheel-clear", "api/firstrun/dismiss"):
            try:
                return getattr(self, "_post_" + r[4:].replace("-", "_").replace("/", "_"))(body)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                app.say("ERROR in " + r + ":\n" + traceback.format_exc())
                return self._json({"ok": False, "error": friendly(e)}, 500)
        if r in coach2.POST_ROUTES:         # Coach 2.0
            return self._carsetup_call(lambda: coach2.post(self, r, body), r)
        if r in carsetup.POST_ROUTES:       # 2.15 Car and setup
            return self._carsetup_call(lambda: carsetup.post(self, r, body), r)
        if r == "api/settings":
            try:
                view = settings_io.save(body)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except SystemExit as e:
                return self._json({"ok": False, "error": friendly(e)}, 400)
            except OSError as e:
                app.say(f"ERROR saving settings: {type(e).__name__}: {e}")   # the detail goes to the log only
                return self._json({"ok": False, "error": SETTINGS_WRITE_ERROR}, 500)
            what = [w + " updated" for w, k in (("token", "token"), ("Discord webhook", "discord_webhook")) if body.get(k)]
            if body.get("clear_discord") is True:
                what.append("Discord webhook removed")
            if body.get("clear_discord") is True or body.get("discord_webhook"):
                self.server.discord_info = None   # a new webhook: its name is learnt again by Check
            # never the values themselves: only which secrets changed
            app.say("Settings saved from the desktop app" + (f" ({', '.join(what)})." if what else "."))
            return self._json({"ok": True, "settings": view})
        if r == "api/open-folder":
            lib = self._library_folder()
            if lib is None:
                return self._json({"ok": False, "error": NO_TOKEN_SAVED}, 400)
            lib.mkdir(parents=True, exist_ok=True)
            try:
                import os
                os.startfile(str(lib))  # type: ignore[attr-defined]  (Windows only)
            except (AttributeError, OSError):
                return self._json({"ok": False, "error": f"Your reviews are in {lib}"})
            return self._json({"ok": True})
        return self._nf()

    # ---------- POST endpoints for the new views
    def _post_sync(self, body):
        """Start a history sync. auto=true (the window just opened) only syncs when it's switched on,
        a token is saved, nothing else is running and the last sync is over 10 minutes old."""
        try:
            cfg = self._cfg()
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        if body.get("auto") is True:
            api = self._cache_api(cfg)
            if api is None or not cfg.get("sync_on_open", True) or self.server.jobs.running:
                return self._json({"ok": True, "skipped": True})
            from .. import sync
            if not sync.due(sync.Store.for_api(api)):
                return self._json({"ok": True, "skipped": True})
        elif self.server.jobs.kind == "sync":
            return self._json({"ok": True, "id": self.server.jobs.snapshot().get("id"), "already": True})
        return self._json(self.server.jobs.start("sync"))

    def _post_prep(self, body):
        tid, cid = _pos_int(body.get("track_id"), "track"), _pos_int(body.get("car_id"), "car")
        self._cfg()
        return self._json(self.server.jobs.start("prep", {"track_id": tid, "car_id": cid}))

    MAX_PDF = 40 * 1024 * 1024

    def _post_schedule_upload(self):
        """The season PDF you picked, sent as the raw request body (application/pdf). Saved to a temp file,
        then read by a background job."""
        cl = (self.headers.get("Content-Length") or "").strip()
        if not cl.isdigit() or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._json({"ok": False, "error": "Bad request."}, 400)
        n = int(cl)
        if n > self.MAX_PDF or n < 1000:
            self.close_connection = True
            return self._json({"ok": False, "error": "That file isn't the season schedule PDF."}, 400)
        if self.server.jobs.running or self.server.jobs.closing:   # don't take 40 MB we can't use
            return self._json({"ok": False, "error": "Wait for " + self.server.jobs.snapshot().get(
                "title", "the current job").lower() + " to finish first."})
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="debrief-sched-"))
        p = d / "upload.pdf"
        left = n
        with open(p, "wb") as f:
            while left:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        self._consumed = not left
        def drop():
            try:
                p.unlink()
                d.rmdir()
            except OSError:
                pass
        if left:
            drop()
            self.close_connection = True
            return self._json({"ok": False, "error": "The file didn't arrive in full. Try again."}, 400)
        with open(p, "rb") as f:
            if f.read(5) != b"%PDF-":
                drop()
                return self._json({"ok": False, "error": "That file isn't a PDF. Pick iRacing's season schedule PDF."}, 400)
        res = self.server.jobs.start("schedule", {"path": str(p)})
        if not res.get("ok"):
            drop()
        return self._json(res)

    def _post_schedule(self, body):
        if body.get("action") != "download":
            raise ValueError("Bad request.")
        return self._json(self.server.jobs.start("schedule", {}))

    def _post_series(self, body):
        cfg = self._cfg()
        from .. import schedule
        series = self._module("series")
        sched = schedule.load_current()
        if sched is None:
            raise ValueError("Load the season schedule first.")
        mine = series.save(cfg["library_folder"], body.get("ids"), sched)
        return self._json({"ok": True, "mine": mine})

    def _post_prepare_choice(self, body):
        cfg = self._cfg()
        return self._json({"ok": True, "choice": self._module("series").save_choice(cfg["library_folder"], body)})

    def _post_notes(self, body):
        cfg = self._cfg()
        notes = self._module("notes")
        try:
            card = self._save_card(notes, cfg, notes.clean(body.get("card")))
        except ValueError as e:
            conflict_cls = getattr(notes, "Conflict", None)
            msg = str(e)
            if (isinstance(conflict_cls, type) and isinstance(e, conflict_cls)) or "changed since" in msg:
                # the card was saved elsewhere after the window opened it: the window reloads it
                return self._json({"ok": False, "error": msg, "conflict": True}, 400)
            raise
        return self._json({"ok": True, "card": card})

    def _post_strategy(self, body):
        cfg = self._cfg()
        strategy = self._module("strategy")
        act = body.get("action")
        if act == "plan":
            inputs = body.get("inputs")
            if not isinstance(inputs, dict) or len(inputs) > 20:
                raise ValueError("Fill in the race length, lap time, fuel use and tank size.")
            return self._json({"ok": True, "plan": strategy.plan(dict(inputs))})
        if act == "save_car":
            car = _short_text(body.get("car"), "car")
            tank = _num_in(body.get("tank_l"), 1, 200, "The tank size must be between 1 and 200 litres.")
            loss = _num_in(body.get("pit_loss_s"), 0, 300, "The pit stop loss must be between 0 and 300 seconds.")
            track = _short_text(body.get("track"), "track") if body.get("track") not in (None, "") else None
            if tank is None and loss is None:
                raise ValueError("Nothing to save.")
            if loss is not None and track is None:
                raise ValueError("Pick a track for the pit stop loss.")
            saved = strategy.save_car(cfg["library_folder"], car, tank_l=tank, pit_loss_s=loss, track=track)
            return self._json({"ok": True, "car": saved})
        raise ValueError("Bad request.")

    MAX_INTEL = 5 * 1024 * 1024
    MIN_XLSX = 1024
    MIN_CSV = 16            # a short Track Intel CSV (a few rows) is legitimate; xlsx keeps the 1 KB floor
    INTEL_NAME_OK = re.compile(r"^[^\x00-\x1f/\\<>:\"|?*]{1,160}\.(xlsx|csv)$", re.I)

    def _post_intel_upload(self):
        """A Track Intel sheet (.xlsx or .csv) as the raw request body (application/octet-stream), with the
        file name in ?name=. Parsed in memory and kept for 30 minutes under a random id."""
        cl = (self.headers.get("Content-Length") or "").strip()
        if not cl.isdigit() or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._json({"ok": False, "error": "Bad request."}, 400)
        n = int(cl)
        name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if n > self.MAX_INTEL:
            self.close_connection = True
            return self._json({"ok": False, "error": "That file is too big. A Track Intel sheet is well under 5 MB."}, 400)
        if not self.INTEL_NAME_OK.match(name) or ctype != "application/octet-stream":
            self.close_connection = True
            return self._json({"ok": False, "error": "Pick an .xlsx or .csv file."}, 400)
        is_xlsx = name.lower().endswith(".xlsx")
        if n < (self.MIN_XLSX if is_xlsx else self.MIN_CSV):
            self.close_connection = True
            return self._json({"ok": False, "error": "That file is too small to be a Track Intel sheet (it's under 1 KB)."}, 400)
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) != n:
            self.close_connection = True
            return self._json({"ok": False, "error": "The file didn't arrive in full. Try again."}, 400)
        self._consumed = True
        data = bytes(buf)
        head = data[:8]
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return self._json({"ok": False, "error": "That's an old .xls or a password-protected file. Save it as "
                                                     ".xlsx or CSV and try again."}, 400)
        if is_xlsx and not head.startswith(b"PK\x03\x04"):
            return self._json({"ok": False, "error": "That file isn't a real .xlsx. Save it as .xlsx or CSV and try again."}, 400)
        if not is_xlsx and (head.startswith(b"PK\x03\x04") or b"\x00" in data[:4096]):
            return self._json({"ok": False, "error": "That file isn't a CSV text file. Save it as CSV and try again."}, 400)
        try:
            intel = self._module("intel")
            try:
                with self.server.intel.parse_lock:
                    parsed = intel.parse_upload(data, name)
            except ValueError:
                raise
            except Exception as e:  # noqa: BLE001  (XlsxError and friends carry plain messages)
                if type(e).__name__ in ("XlsxError", "IntelError"):
                    raise ValueError(str(e))
                raise
            sheets = (parsed or {}).get("sheets") or []
            if not sheets:
                raise ValueError("Debrief couldn't find a table in that file.")
            uid = self.server.intel.add(parsed, name)
            out = [{"name": str(sh.get("name") or f"Sheet {i + 1}")[:80], "header": list(sh.get("header") or [])[:40],
                    "rows": [list(r)[:40] for r in (sh.get("rows") or [])[:10]],
                    "n_rows": len(sh.get("rows") or []),
                    "suggest": {str(k): v for k, v in (sh.get("suggest") or {}).items()}}
                   for i, sh in enumerate(sheets[:20])]
            app.say(f"Track Intel sheet loaded ({len(out)} sheet(s)).")
            return self._json({"ok": True, "upload_id": uid, "sheets": out})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            app.say("ERROR reading a Track Intel sheet:\n" + traceback.format_exc())
            return self._json({"ok": False, "error": friendly(e)}, 500)

    def _known_tracks(self, cfg) -> list:
        """Track names Debrief already knows: schedule, library, synced sessions and Garage 61's cached list."""
        names = set()
        try:
            from .. import schedule
            sched = schedule.load_current()
            for ser in (sched or {}).get("series") or []:
                names.update(w.get("track") for w in ser.get("weeks") or [])
        except Exception:  # noqa: BLE001
            pass
        lib = Path(cfg["library_folder"])
        names.update(r.get("track") for r in self.server.profile_cache.rows(lib))
        api = self._cache_api(cfg)
        names.update(s.get("track") for s in self._synced_sessions(api))
        for t in (api.cached_items("tracks") if api else None) or []:
            if isinstance(t, dict):
                names.add(app.item_name(t))
        return sorted(n for n in names if isinstance(n, str) and n.strip())

    def _post_intel_preview(self, body):
        cfg = self._cfg()
        intel = self._module("intel")
        it, sh, mapping, mode = clean_intel_request(body, self.server.intel)
        res = intel.preview(it["parsed"], sh, mapping, mode, cfg, self._known_tracks(cfg))
        return self._json({"ok": True, "preview": res})

    def _post_intel_import(self, body):
        if body.get("confirm") is not True:
            raise ValueError("Check the preview, then press Import.")
        cfg = self._cfg()
        intel = self._module("intel")
        it, sh, mapping, mode = clean_intel_request(body, self.server.intel)
        res = intel.apply(it["parsed"], sh, mapping, mode, cfg, self._known_tracks(cfg))
        self.server.intel.drop(body.get("upload_id"))
        app.say(f"Track Intel import: {res}")
        return self._json({"ok": True, **(res if isinstance(res, dict) else {})})

    @staticmethod
    def _save_card(notes, cfg, card):
        """notes.save with the settings (2.4: it also writes your shared team file when sharing is on). A notes
        module without the cfg parameter (before 24-B's merge) is called the 2.3 way."""
        import inspect
        try:
            takes_cfg = "cfg" in inspect.signature(notes.save).parameters
        except (TypeError, ValueError):
            takes_cfg = False
        return notes.save(cfg["library_folder"], card, cfg=cfg) if takes_cfg else notes.save(cfg["library_folder"], card)

    MAX_STAGE = 4 * 1024 * 1024
    MIN_STAGE = 1024

    def _post_discord_stage(self):
        """The share card to preview, as a raw image/png body (1 KB to 4 MB, the schedule-upload Content-Length
        rules). Checked (PNG signature, IHDR size) and kept in memory under a single-use nonce for 10 minutes.
        No network: nothing is sent to Discord until api/discord-post."""
        cl = (self.headers.get("Content-Length") or "").strip()
        if not cl.isdigit() or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._json({"ok": False, "error": "Bad request."}, 400)
        n = int(cl)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if n > self.MAX_STAGE or n < self.MIN_STAGE or ctype != "image/png":
            self.close_connection = True
            return self._json({"ok": False, "error": "That picture can't be posted. Make the preview again."}, 400)
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) != n:
            self.close_connection = True
            return self._json({"ok": False, "error": "The picture didn't arrive in full. Try again."}, 400)
        self._consumed = True
        png = bytes(buf)
        size = png_size(png)
        if size is None:
            return self._json({"ok": False, "error": "That picture can't be posted. Make the preview again."}, 400)
        nonce = self.server.discord_stage.add(png, *size)
        hook = settings_io.discord_webhook()
        info = self.server.discord_info or {}
        hint = settings_io.webhook_hint(hook) if hook else None
        target = {"saved": bool(hook), "hint": hint,
                  "name": info.get("name") if hint and info.get("hint") == hint else None}
        return self._json({"ok": True, "nonce": nonce, "width": size[0], "height": size[1], "target": target})

    def _post_discord_post(self, body):
        """Post a staged share card to the saved Discord webhook: 1 Discord request. Runs only from the Post
        button in the window's preview dialog, with the nonce api/discord-stage gave it (single use: it is
        taken before anything else, so a repeat or a stale preview is refused). A running job doesn't matter:
        posting never touches the library. THIS IS THE ONLY PLACE THAT CALLS discord.post."""
        item = self.server.discord_stage.pop(body.get("nonce"))
        if item is None:
            raise ValueError("Make the preview again.")
        content = body.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str) or len(content) > 2000:
            raise ValueError("The message can be up to 2000 characters. Make the preview again.")
        hook = settings_io.discord_webhook()
        if not hook:
            raise ValueError("Add your team's Discord webhook in Settings first.")
        discord = self._module("discord")
        hint = settings_io.webhook_hint(hook)
        try:
            res = discord.post(hook, item["png"], content)
        except Exception as e:  # noqa: BLE001  (DiscordError has a plain message; anything else stays vague)
            msg = redact_webhook(str(e), hook)
            app.say(f"Discord post failed (webhook {hint}): {type(e).__name__}: {msg}"[:400])
            if type(e).__name__ == "DiscordError" and msg.strip():   # plain and redacted already (25-B)
                return self._json({"ok": False, "error": msg}, 400)
            return self._json({"ok": False, "error": "Couldn't post to Discord. Details were saved to debrief.log in "
                                                     + app.data_folder_label() + "."}, 502)
        app.say(f"Posted to Discord (webhook {hint}).")
        mid = res.get("id") if isinstance(res, dict) else None
        return self._json({"ok": True, "id": str(mid) if isinstance(mid, (str, int)) else None})

    def _post_discord_check(self, body):
        """Settings > Discord > Check: which webhook and channel posts would go to. 1 Discord request."""
        hook = settings_io.discord_webhook()
        if not hook:
            raise ValueError("Paste your team's Discord webhook and save it first.")
        discord = self._module("discord")
        hint = settings_io.webhook_hint(hook)
        try:
            res = discord.check(hook)
        except Exception as e:  # noqa: BLE001
            msg = redact_webhook(str(e), hook)
            app.say(f"Discord check failed (webhook {hint}): {type(e).__name__}: {msg}"[:400])
            if type(e).__name__ == "DiscordError" and msg.strip():
                return self._json({"ok": False, "error": msg}, 400)
            return self._json({"ok": False, "error": "Couldn't reach Discord."}, 502)
        res = res if isinstance(res, dict) else {}
        name = res.get("name")
        name = redact_webhook(name, hook)[:80] if isinstance(name, str) and name.strip() else None
        ch = res.get("channel_id")
        ch = str(ch) if isinstance(ch, (str, int)) and re.match(r"^\d{1,25}$", str(ch)) else None
        if not res.get("ok", True):
            return self._json({"ok": False, "error": "That Discord webhook no longer works. Paste a new one in Settings."}, 502)
        self.server.discord_info = {"hint": hint, "name": name, "channel_id": ch}
        app.say(f"Checked the Discord webhook ({hint}).")
        return self._json({"ok": True, "name": name, "channel_id": ch, "hint": hint})

    SERIES_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")

    def _post_weekly(self, body):
        """Start the weekly leaderboards refresh: {series_ids?: [str], force_old?: bool}."""
        ids = body.get("series_ids")
        if ids is not None:
            if not isinstance(ids, list) or len(ids) > 50 or not all(
                    isinstance(x, str) and self.SERIES_ID_OK.match(x) for x in ids):
                raise ValueError("Pick your series from the list.")
            ids = list(dict.fromkeys(ids))
        fo = body.get("force_old", False)
        if not isinstance(fo, bool):
            raise ValueError("Bad request.")
        self._cfg()
        if app.optional_module("leaderboards") is None:
            raise ValueError("Weekly leaderboards aren't available right now. Details were saved to debrief.log in "
                             + app.data_folder_label() + ".")
        return self._json(self.server.jobs.start("weekly", {"series_ids": ids, "force_old": fo}))

    def _post_team(self, body):
        self._cfg()
        return self._json(self.server.jobs.start("team"))

    def _post_goals(self, body):
        cfg = self._cfg()
        goals = self._module("goals")
        act = body.get("action")
        if act == "save":
            g = goals.save_goal(cfg["library_folder"], clean_goal(body.get("goal")))
            _rebuild_library(cfg)
            return self._json({"ok": True, "goal": g})
        if act == "delete":
            gid = body.get("id")
            if not isinstance(gid, str) or not GOAL_ID_OK.match(gid):
                raise ValueError("Bad request.")
            deleted = bool(goals.delete_goal(cfg["library_folder"], gid))
            if deleted:
                _rebuild_library(cfg)
            return self._json({"ok": True, "deleted": deleted})
        raise ValueError("Bad request.")

    MAX_RESULT_CSV = 2 * 1024 * 1024

    def _post_result_csv(self):
        """iRacing's results CSV as the raw body, ?review=<folder>. The result form is filled in and the person presses
        Save; since 2.18 the parsed table (cars, classes, iRatings, lap times) is also kept beside the review as
        field.json for Car vs car (carcompare.py). Nothing else is saved."""
        cl = (self.headers.get("Content-Length") or "").strip()
        if not cl.isdigit() or self.headers.get("Transfer-Encoding") or int(cl) > self.MAX_RESULT_CSV:
            self.close_connection = True
            return self._json({"ok": False, "error": "That file is too big for a results CSV."}, 400)
        data = self.rfile.read(int(cl))
        from .. import results_csv
        try:
            q = parse_qs(urlsplit(self.path).query)
            d = self._review_dir(self._cfg(), (q.get("review") or [""])[0])
            parsed = results_csv.parse(data)
            review = json.loads((d / "review.json").read_text(encoding="utf-8"))
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except OSError:
            return self._json({"ok": False, "error": "Couldn't read that review."}, 400)
        if not isinstance(review, dict):
            return self._json({"ok": False, "error": "That review's data file is damaged."}, 400)
        me = results_csv.pick(parsed, (review.get("meta") or {}).get("driver"))
        _save_field(d, review, parsed)
        return self._json({"ok": True, "matched": me, "drivers": parsed["drivers"], "multiclass": parsed["multiclass"]})

    def _post_racecap_link(self, body):
        cfg = self._cfg()
        d = self._review_dir(cfg, body.get("review"))
        folder = str(cfg.get("racecap_library") or "").strip()
        if not folder:
            raise ValueError("Pick RaceCap's folder in Settings first.")
        sub = str(body.get("subsession") or "").strip()
        if sub and not re.fullmatch(r"\d{1,12}", sub):
            raise ValueError("A race ID is a number, like 89132385.")
        if self.server.jobs.running:
            raise ValueError("Wait for " + self.server.jobs.snapshot().get("title", "the current job").lower() + " to finish first.")
        from .. import racecap_link
        api = None if sub else app.make_api(cfg)
        return self._json({"ok": True, **racecap_link.link_review(cfg["library_folder"], d, folder, api, subsession=sub or None, log=app.say)})

    def _get_markers(self, q):
        cfg = self._cfg()
        d = self._review_dir(cfg, q.get("review"))
        from .. import markers
        data = json.loads((d / "review.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("That review's data file is damaged.")
        rows, exact = markers.items(data)
        if not rows:
            raise ValueError("This review has no moments to mark yet.")
        return self._json({"ok": True, "edl": markers.edl(data), "name": re.sub(r"[^\w.-]+", "_", d.name)[:80] + "_markers.edl",
                           "exact": exact})

    def _post_result(self, body):
        cfg = self._cfg()
        d = self._review_dir(cfg, body.get("review"))
        results = self._module("results")
        act = body.get("action", "save")
        if self.server.jobs.running:
            raise ValueError("Wait for " + self.server.jobs.snapshot().get("title", "the current job").lower() +
                             " to finish first.")
        if act == "save":
            res = results.save(cfg["library_folder"], d.name, clean_result_fields(body.get("fields")))
            return self._json({"ok": True, "result": res})
        if act == "delete":
            return self._json({"ok": True, "deleted": bool(results.delete(cfg["library_folder"], d.name))})
        raise ValueError("Bad request.")


    # ---------- Setup Lab (2.12): read only, from the telemetry iRacing saves on this PC
    def _setuplab_index(self, cfg):
        return self.server.setuplab.load(setuplab_cache_path(cfg))

    def _get_setuplab_combos(self, q):
        """The cars and tracks in the saved index, newest first, and where the telemetry folder is. Never reads a
        telemetry file. With car= and track= (a Prepare view) only the matching combo is worked out."""
        cfg = self._cfg()
        setuplab = self._module("setuplab")
        folder = settings_io.telemetry_folder(cfg)
        try:
            folder_ok = folder.is_dir()
        except OSError:
            folder_ok = False
        idx, keys = self._setuplab_index(cfg)
        combos = setuplab.combos(idx) if idx else []
        if q.get("car") is not None or q.get("track") is not None:
            car, track = _short_text(q.get("car"), "car"), _short_text(q.get("track"), "track")
            return self._json(_jsonable({"ok": True, "match": match_setuplab_combo(combos, car, track) if folder_ok else None}))
        saved = settings_io.load_extra("setuplab").get("series") or {}
        for c in combos:
            v = saved.get(series_key(c.get("car_path") or "", c.get("track_code") or "")) if isinstance(saved, dict) else None
            c["series_override"] = v if v in ("open", "fixed") else None
        return self._json(_jsonable({
            "ok": True, "folder": str(folder), "folder_ok": folder_ok, "folder_set": bool(str(cfg.get("iracing_folder") or "").strip()),
            "indexed": idx is not None, "files": len(idx["files"]) if idx else 0, "bad": len(idx["bad"]) if idx else 0,
            "unread": unread_files(folder, keys) if folder_ok else None, "combos": combos,
            "running": self.server.jobs.kind == "setuplab"}))

    def _get_setuplab_report(self, q):
        """Setup Lab for one car + track from the saved index (fast: no telemetry file is opened)."""
        cfg = self._cfg()
        setuplab = self._module("setuplab")
        car, track = q.get("car"), q.get("track")
        if not isinstance(car, str) or not IDENT_OK.match(car) or not isinstance(track, str) or not IDENT_OK.match(track):
            raise ValueError("Pick a car and track.")
        sid = q.get("series") or None
        if sid is not None and not self.SERIES_ID_OK.match(sid):
            raise ValueError("Pick a series from the list.")
        idx, _keys = self._setuplab_index(cfg)
        if idx is None:
            raise ValueError("Read your telemetry first.")
        folder = settings_io.telemetry_folder(cfg)
        combo = next((c for c in setuplab.combos(idx) if (c.get("car_path") or "").lower() == car.lower()
                      and (c.get("track_code") or "").lower() == track.lower()), None)
        saved = settings_io.load_extra("setuplab").get("series") or {}
        override = saved.get(series_key(car, track)) if isinstance(saved, dict) else None
        override = override if override in ("open", "fixed") else None
        kind, source = (override, "you set this") if override else schedule_series_setup(combo, sid)
        rep = setuplab.report(folder, car, track, series_setup=kind, idx=dict(idx, missing_folder=not folder.is_dir()))
        if kind:
            rep["series_setup_source"] = source
        rep["series_override"] = override
        return self._json(_jsonable({"ok": True, "report": rep}))

    def _post_setuplab_read(self, body):
        cfg = self._cfg()
        self._module("setuplab")
        folder = settings_io.telemetry_folder(cfg)
        if not folder.is_dir():
            raise ValueError(NO_TELEMETRY.format(folder=folder))
        return self._json(self.server.jobs.start("setuplab"))

    def _post_setuplab_stop(self, body):
        if self.server.jobs.kind == "setuplab":
            self.server.jobs.stop_scan.set()
        return self._json({"ok": True})

    def _post_setuplab_series(self, body):
        """Open, fixed or "" (let Debrief tell) for one car + track, saved next to settings.toml."""
        car, track, v = body.get("car"), body.get("track"), body.get("setup")
        if not isinstance(car, str) or not IDENT_OK.match(car) or not isinstance(track, str) or not IDENT_OK.match(track):
            raise ValueError("Pick a car and track.")
        if v not in ("open", "fixed", "", None):
            raise ValueError("Pick open, fixed or not sure.")
        data = settings_io.load_extra("setuplab")
        series = data.get("series") if isinstance(data.get("series"), dict) else {}
        k = series_key(car, track)
        series.pop(k, None)
        if v:
            if len(series) >= MAX_SERIES_CHOICES:
                raise ValueError("Too many saved choices.")
            series[k] = v
        data["series"] = series
        try:
            settings_io.save_extra("setuplab", data)
        except OSError as e:
            app.say(f"ERROR saving Setup Lab's series choice: {type(e).__name__}: {e}")
            raise ValueError(SETTINGS_WRITE_ERROR)
        return self._json({"ok": True, "setup": v or None})

    MAX_SETUP_FILE = 50 * 1024 * 1024
    SETUP_NAME_OK = re.compile(r"^[^\x00-\x1f/\\<>:\"|?*]{1,160}$")

    def _post_setuplab_check_file(self):
        """A setup file from someone else (.sto or .zip), as the raw body with its name in ?name=. Checked for
        programs and scripts with setuplab.check_setup_file, which only reads: nothing is extracted or run. The
        check needs a file on disk, so it sits in a private temp folder for the check and is deleted straight
        after; Debrief never copies it anywhere else (and never into iRacing)."""
        cl = (self.headers.get("Content-Length") or "").strip()
        name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
        if not cl.isdigit() or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._json({"ok": False, "error": "Bad request."}, 400)
        n = int(cl)
        if n > self.MAX_SETUP_FILE:
            self.close_connection = True
            return self._json({"ok": False, "error": "That file is far too big to be a setup (over 50 MB)."}, 400)
        if not self.SETUP_NAME_OK.match(name) or name.strip(". ") == "":
            self.close_connection = True
            return self._json({"ok": False, "error": "Pick a setup file (.sto or .zip)."}, 400)
        setuplab = app.optional_module("setuplab")
        if setuplab is None:
            self.close_connection = True
            return self._json({"ok": False, "error": "Setup Lab isn't available right now."}, 500)
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="debrief-setupcheck-"))
        try:
            # saved under a fixed name: his file's name is only shown, never used as a path (and the check reads the
            # name it is given for the hidden-type tricks, e.g. setup.exe.sto)
            suf = Path(name).suffix.lower()
            p = d / ("upload" + (suf if re.fullmatch(r"\.[a-z0-9]{1,10}", suf) else ""))
            left = n
            with open(p, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            self._consumed = not left
            if left:
                self.close_connection = True
                return self._json({"ok": False, "error": "The file didn't arrive in full. Try again."}, 400)
            try:
                res = setuplab.check_setup_file(p, name=name)
            except Exception:  # noqa: BLE001
                app.say("ERROR checking a setup file:\n" + traceback.format_exc())
                return self._json({"ok": False, "error": "Debrief couldn't check that file. Don't use it."}, 500)
            res["file"] = name
            app.say(f"Checked a setup file: {'no problems' if res.get('ok') else str(len(res.get('problems') or [])) + ' problem(s)'}.")
            return self._json(_jsonable({"ok": True, "check": res}))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---------- the live coach (2.12): one runner on the server, never a job
    def _get_coach_status(self, q):
        return self._json({"ok": True, **self.server.coach_status()})

    def _post_coach_start(self, body):
        self._module("livecoach")
        res = self.server.start_coach()
        return self._json({**res, "status": self.server.coach_status()} if res.get("ok") else res, 200 if res.get("ok") else 400)

    def _post_coach_stop(self, body):
        self.server.stop_coach()
        return self._json({"ok": True, "status": self.server.coach_status()})

    def _post_coach_mute(self, body):
        with self.server.coach_lock:
            r = self.server.coach
        if r is None or not r.running():
            raise ValueError("Start the coach first.")
        muted = bool(r.toggle())
        return self._json({"ok": True, "muted": muted})

    def _post_coach_settings(self, body):
        livecoach = self._module("livecoach")
        new = clean_coach_settings(body.get("settings"))
        merged = livecoach.Settings.from_dict({**saved_coach_settings(), **new}).to_dict()
        keep = {k: merged[k] for k in COACH_FIELDS}
        try:
            with self.server.coach_json_lock:
                data = settings_io.load_extra("coach")
                data["settings"] = keep
                settings_io.save_extra("coach", data)
        except OSError as e:
            app.say(f"ERROR saving the coach settings: {type(e).__name__}: {e}")
            raise ValueError(SETTINGS_WRITE_ERROR)
        with self.server.coach_lock:
            r = self.server.coach
        if r is not None:
            try:
                r.update_settings(new)
            except Exception:  # noqa: BLE001
                app.say("ERROR applying the coach settings:\n" + traceback.format_exc())
        return self._json({"ok": True, "settings": keep})

    def _post_coach_test_voice(self, body):
        self._module("voice")
        res = self.server.coach_test_voice()
        return self._json(res, 200 if res.get("ok") else 500)

    # ---------- Practice (2.13): read only, from the telemetry iRacing saves on this PC
    def _get_sessions_ibt(self, q):
        """The telemetry sessions on this PC, newest first, from Setup Lab's saved index (no telemetry file is read
        in full). Before the first read: indexed false, and the page offers Read my telemetry."""
        cfg = self._cfg()
        folder = settings_io.telemetry_folder(cfg)
        try:
            folder_ok = folder.is_dir()
        except OSError:
            folder_ok = False
        ss, idx = self.server.practice_sessions(cfg)
        _idx, keys = self._setuplab_index(cfg)
        keep = ("id", "car", "car_path", "track", "track_code", "date", "last", "sort", "subsession", "files",
                "session_types", "laps", "valid_laps", "clean_laps", "best", "median", "surface_temp")
        return self._json(_jsonable({
            "ok": True, "folder": str(folder), "folder_ok": folder_ok, "indexed": idx is not None,
            "unread": unread_files(folder, keys) if folder_ok else None, "running": self.server.jobs.kind == "setuplab",
            "sessions": [{k: x.get(k) for k in keep} for x in ss]}))

    def _practice_session(self, cfg, q) -> tuple:
        sid = q.get("id")
        if not isinstance(sid, str) or not SESSION_ID_OK.match(sid):
            raise ValueError("That session wasn't found.")
        ss, idx = self.server.practice_sessions(cfg)
        if idx is None:
            raise ValueError("Read your telemetry first.")
        me = next((x for x in ss if x.get("id") == sid), None)
        if me is None:
            raise ValueError("That session wasn't found. If it is new, press Read my telemetry.")
        return me, ss, idx

    def _get_session_review(self, q):
        """One session reviewed (sessionibt.review), with the corner coach for its car and track over the recent
        files up to this session. Worked out once per set of files, then read from the cache."""
        cfg = self._cfg()
        si = self._module("sessionibt")
        me, ss, idx = self._practice_session(cfg, q)
        earlier = [{k: v for k, v in x.items() if k != "paths"} for x in ss
                   if _same_combo(x, me["car_path"], me["track_code"]) and x["sort"] < me["sort"]]
        hist = [[x.get("id"), x.get("best"), x.get("median")] for x in earlier]
        try:
            key = practice_key("review", me.get("paths") or [], hist)
        except OSError:
            raise ValueError("Some of this session's telemetry files have gone. Press Read my telemetry again.")
        rv = self.server.practice.get(cfg["cache_dir"], key,
                                      lambda: si.review(me.get("paths") or [], history=earlier, idx=idx))
        cards = self.server.corner_coach_for(cfg, ss, me["car_path"], me["track_code"], upto=me["sort"])
        sess = {k: v for k, v in me.items() if k != "paths"}
        return self._json(_jsonable({"ok": True, "session": sess, "review": rv, "corners": cards,
                                     "latest": ss[0].get("id") == me.get("id") if ss else False,
                                     "one_thing": self._session_one_thing(cfg, me, ss)}))

    def _session_one_thing(self, cfg, me, ss) -> dict | None:
        """2.14: the practice plan scored on this session: scored now when this car + track's plan is waiting and
        this session is newer than it (auto-score), else the score saved for it before. None when there's none.
        A problem here never stops the session page."""
        if app.optional_module("practiceplan") is None:
            return None
        try:
            got = self.server.score_session(cfg, me, ss, auto=True)
            if got is not None:
                return {"plan": got["plan"], "next_plan": got["next_plan"], "just_scored": True}
            p = self.server.session_score(me.get("car_path"), me.get("track_code"), me.get("id"))
            return {"plan": p, "just_scored": False} if p else None
        except Exception:  # noqa: BLE001
            app.say("ERROR scoring the practice plan:\n" + traceback.format_exc())
            return None

    # ---------- Practice loop (2.14): one thing this session
    def _plan_combo(self, cfg, q) -> tuple:
        """(car path, track code, this combo's sessions, every session, index) from ?car=&track=, or the newest
        session's car and track when neither is given. Raises ValueError (plain words)."""
        self._module("practiceplan")
        car, track = q.get("car"), q.get("track")
        ss, idx = self.server.practice_sessions(cfg)
        if car is None and track is None:
            if idx is None or not ss:
                return None, None, [], ss, idx
            car, track = ss[0].get("car_path"), ss[0].get("track_code")
        if not isinstance(car, str) or not IDENT_OK.match(car) or not isinstance(track, str) or not IDENT_OK.match(track):
            raise ValueError("Pick a car and track.")
        return car, track, [x for x in ss if _same_combo(x, car, track)], ss, idx

    @staticmethod
    def _combos(ss) -> list:
        out, seen = [], set()
        for x in ss:
            k = (str(x.get("car_path") or "").lower(), str(x.get("track_code") or "").lower())
            if k in seen or not k[0]:
                continue
            seen.add(k)
            out.append({"car_path": x.get("car_path"), "track_code": x.get("track_code"), "car": x.get("car"),
                        "track": x.get("track"), "date": x.get("date")})
            if len(out) >= 30:
                break
        return out

    def _plan_reply(self, cfg, q, fresh=False):
        car, track, mine, ss, idx = self._plan_combo(cfg, q)
        base = {"ok": True, "indexed": idx is not None, "combos": self._combos(ss) if idx is not None else []}
        if idx is None:
            return dict(base, plan=None, message="Read your telemetry first. Then Debrief picks one thing for you to "
                                                  "work on.")
        if not car:
            return dict(base, plan=None, message="No telemetry sessions yet. Drive a few laps with telemetry on, then "
                                                  "read your telemetry.")
        if not mine:
            return dict(base, plan=None, car_path=car, track_code=track, car=car, track=track,
                        message="No telemetry sessions of this car at this track yet.")
        cur, plans = self.server.current_plan(cfg, car, track, fresh=fresh)
        last = next((p for p in plans if isinstance(p.get("score"), dict)), None)
        return dict(base, car_path=car, track_code=track, car=mine[0].get("car") or car,
                    track=mine[0].get("track") or track, plan=plan_view(cur), last=plan_view(last),
                    history=[plan_brief(p) for p in plans[:20]], sessions=len(mine))

    def _get_plan(self, q):
        """The current practice plan for a car + track (made from the latest Corner coach report when there's none,
        or when the last one has been scored), the last scored one and the history."""
        return self._json(_jsonable(self._plan_reply(self._cfg(), q)))

    def _get_plan_history(self, q):
        cfg = self._cfg()
        car, track, mine, _ss, idx = self._plan_combo(cfg, q)
        pp = self._module("practiceplan")
        plans = pp.load_plans(plans_folder(), car, track) if car else []
        return self._json(_jsonable({"ok": True, "car_path": car, "track_code": track,
                                     "history": [plan_brief(p) for p in plans]}))

    def _post_plan_new(self, body):
        """Pick another: a fresh plan from the latest Corner coach report (the current one stays in the history)."""
        b = body if isinstance(body, dict) else {}
        q = {k: b[k] for k in ("car", "track") if k in b}
        return self._json(_jsonable(self._plan_reply(self._cfg(), q, fresh=True)))

    def _post_plan_score(self, body):
        """Score this car + track's current plan on one telemetry session (?session= or {"session"})."""
        self._module("practiceplan")
        cfg = self._cfg()
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        sid = q.get("session") or (body.get("session") if isinstance(body, dict) else None)
        me, ss, _idx = self._practice_session(cfg, {"id": sid})
        got = self.server.score_session(cfg, me, ss, auto=False)
        return self._json(_jsonable({"ok": True, **got}))

    def _get_firstrun(self, q):
        fr = self._module("firstrun")
        cfg = self._cfg()
        return self._json(_jsonable({"ok": True, **fr.walkthrough(cache_file=setuplab_cache_path(cfg))}))

    def _post_firstrun_dismiss(self, body):
        """Hide the first-run checklist ({"dismissed": false} shows it again, from Help)."""
        fr = self._module("firstrun")
        want = not (isinstance(body, dict) and body.get("dismissed") is False)
        if not (fr.dismiss() if want else fr.undismiss()):
            raise ValueError(SETTINGS_WRITE_ERROR)
        cfg = self._cfg()
        return self._json(_jsonable({"ok": True, **fr.walkthrough(cache_file=setuplab_cache_path(cfg))}))

    def _post_coach_wheel_learn(self, body):
        self._module("wheelmute")
        res = self.server.wheel_learn()
        return self._json({**res, "wheel": self.server.wheel_status()}, 200 if res.get("ok") else 400)

    def _post_coach_wheel_clear(self, body):
        self._module("wheelmute")
        res = self.server.wheel_clear()
        return self._json({**res, "wheel": self.server.wheel_status()})

    def _get_corner_coach(self, q):
        """The corner coach for one car + track over its most recent files (what the live coach uses)."""
        cfg = self._cfg()
        self._module("cornercoach")
        car, track = q.get("car"), q.get("track")
        if not isinstance(car, str) or not IDENT_OK.match(car) or not isinstance(track, str) or not IDENT_OK.match(track):
            raise ValueError("Pick a car and track.")
        ss, idx = self.server.practice_sessions(cfg)
        if idx is None:
            raise ValueError("Read your telemetry first.")
        mine = [x for x in ss if _same_combo(x, car, track)]
        rep = self.server.corner_coach_for(cfg, ss, car, track) if mine else corner_report([])
        name = {"car": mine[0]["car"], "track": mine[0]["track"]} if mine else {"car": car, "track": track}
        return self._json(_jsonable({"ok": True, **name, "car_path": car, "track_code": track, "report": rep,
                                     "focus": weak_from_cards(rep)}))


def _save_field(d: Path, review: dict, parsed: dict) -> None:
    """2.18: keep the parsed results table beside the review (field.json, written atomically) so Car vs car can use
    every driver's car, iRating and laps. A failure is logged and never stops the form from filling in."""
    meta = review.get("meta") or {}
    doc = {"version": 1, "track": meta.get("track") or "", "when": meta.get("started_at") or "", "me": meta.get("driver") or "",
           "subsession": str((review.get("racecap") or {}).get("subsession") or ""), "drivers": parsed.get("drivers") or [],
           "multiclass": bool(parsed.get("multiclass"))}
    try:
        tmp = d / "field.json.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, d / "field.json")
    except (OSError, ValueError) as e:
        app.say(f"Couldn't keep the results table for Car vs car ({type(e).__name__}).")
