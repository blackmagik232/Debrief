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
import shutil
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

UI = Path(__file__).resolve().parent / "ui"
ICON_PNG = Path(__file__).resolve().parent / "icon.png"
FALLBACK_TOKENS = """:root{--bg:#f3f4f2;--panel:#fcfcfb;--fg:#0f1214;--fg2:#4b5257;--muted:#7c8388;--grid:#e3e5e1;--axis:#b9bdb8;--band:#eef0ec;
--me:#2a78d6;--ref:#eb6834;--lose:#d03b3b;--good:#0b7d0b;--warn:#a86a00;
--display:"Barlow Condensed","Arial Narrow",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,"Segoe UI",sans-serif;--mono:"JetBrains Mono",ui-monospace,Consolas,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d0f10;--panel:#17191a;--fg:#eef0ee;--fg2:#c0c5c3;--muted:#8a9094;--grid:#272b2c;--axis:#3d4244;--band:#1c1f20;--me:#3987e5;--ref:#d95926;--lose:#e66767;--good:#2fbf4a;--warn:#efb43f;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0d0f10;--panel:#17191a;--fg:#eef0ee;--fg2:#c0c5c3;--muted:#8a9094;--grid:#272b2c;--axis:#3d4244;--band:#1c1f20;--me:#3987e5;--ref:#d95926;--lose:#e66767;--good:#2fbf4a;--warn:#efb43f;color-scheme:dark}
"""
LIB_TYPES = {".html", ".json", ".png", ".svg", ".css", ".js"}
# Fixed types: never Windows' registry (mimetypes reads HKCR, where .js is often text/plain, and with
# nosniff Edge then refuses to run app.js and the window opens blank).
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
# Library pages (review, library, prep, team pages, or any .html someone drops in the folder) run in an
# opaque origin: their scripts work, but they can't call the API, read the window or change settings.
# connect-src 'none' matches the policy in the templates' own <meta> tag: library pages never use the network
# (their data is inline), so a planted page can't send anything anywhere either.
LIB_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads; "
           "connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
# The iframe that shows library pages (ui/index.html) must carry the same flags:
FRAME_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"
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
                "addEventListener('hashchange',say);if(document.readyState==='loading')"
                "addEventListener('DOMContentLoaded',say);else say()})();</script>")
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def ui_csp(html: str) -> str:
    """A strict policy for the window itself. Inline <script> blocks in index.html are allowed by hash,
    so editing them never needs a change here; inline event handlers (onclick=...) are not allowed."""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode() + "'"
                      for m in _INLINE_SCRIPT.findall(html))
    return ("default-src 'self'; script-src 'self'" + (" " + hashes if hashes else "") + "; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
            "worker-src 'self' blob:; "   # the heartbeat worker (app.js) is a blob: script
            "object-src 'none'; base-uri 'none'; form-action 'self'")


def with_bridge(html: bytes) -> bytes:
    tag = FRAME_BRIDGE.encode("utf-8")
    low = html.lower()
    i = low.rfind(b"</body>")
    return html[:i] + tag + html[i:] if i >= 0 else html + tag
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


# ---------------------------------------------------------------- background jobs
JOB_TITLES = {"review": "Reviewing your last race", "check": "Checking the connection",
              "prep": "Building your track prep page", "team": "Updating the team hub",
              "sync": "Syncing your Garage 61 history", "schedule": "Loading the iRacing season schedule",
              "update": "Updating Debrief", "weekly": "Updating this week's team leaderboards"}
BACKGROUND_TITLES = {"refresh": "Updating your saved reviews to the new page layout"}


class Jobs:
    """One background job at a time: a review, a connection check, a prep page, a team refresh or a
    history sync. A sync gives way: starting anything else pauses it (it resumes on the next sync)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.st = {"state": "idle"}
        self.stop_sync = threading.Event()
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
                if not msg.startswith(("Telemetry for lap ", "Telemetry for session ")):
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
        cb(0.97, "Verified. Installing... Debrief will close and reopen in a moment.")
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

    def _close_for_update(self):
        """The installer is running: stop in a few seconds (after the window has seen the result) so it
        can replace Debrief's files. It waits for Debrief to close, installs and reopens Debrief.
        No new job starts meanwhile, and anything still writing (a job or the review refresh) finishes first."""
        self.jobs.closing = True

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

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/{self.secret}/"

    def ping(self):
        self.last_ping = max(self.last_ping, time.monotonic())

    def bye(self):
        # the page is closing (or reloading): exit in ~4 s unless a new ping arrives
        self.last_ping = time.monotonic() - self.timeout_s + 4

    def watchdog(self):
        prev = time.monotonic()
        while not self.stopping.wait(1.0):
            now = time.monotonic()
            if now - prev > 10:  # the PC was asleep: give the window time to ping again
                self.last_ping = max(self.last_ping, now)
            prev = now
            idle = now - self.last_ping
            if idle > self.timeout_s and self.jobs.kind == "sync":
                self.jobs.pause_sync(10.0)   # a sync never keeps Debrief open: it resumes next time
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if "Content-Security-Policy" not in extra:   # library pages carry their own (sandbox) policy
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
        if r in ("api/combos", "api/teams", "api/goals", "api/result", "api/pages", "api/sessions", "api/prepare",
                 "api/notes", "api/update", "api/profile", "api/strategy", "api/weekly", "api/brand"):
            if self.headers.get("X-Debrief") != "1":
                return self._nf()
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                return getattr(self, "_get_" + r[4:])(q)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Garage61Error as e:
                app.say(f"ERROR Garage 61: {e}")
                return self._json({"ok": False, "error": friendly(e)}, 502)
            except Exception as e:  # noqa: BLE001
                app.say("ERROR in " + r + ":\n" + traceback.format_exc())
                return self._json({"ok": False, "error": friendly(e)}, 500)
        return self._nf()

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
            raise ValueError({"prep": "Track prep", "goals": "Goals", "results": "Race results"}.get(name, name) +
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
            try:
                teams = api.teams(refresh=True)   # "Check again" must see a team you've just joined
            except TypeError:
                teams = api.teams()
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
        return self._json({"ok": True, "goals": goals.load(folder), "progress": goals.progress(folder)})

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
        return self._json({"ok": True, "review": d.name, "result": results.load(d)})

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
                body = with_bridge(body)
            return self._send(200, body, ctype, {"Content-Security-Policy": LIB_CSP})
        self._send(200, body, ctype)

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
            if not idx.exists() or (idx.stat().st_mtime < newest and not refreshing):
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
        if r == "api/discord-stage":
            return self._post_discord_stage()
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
        if r == "api/review":
            try:
                params = clean_review_params(body)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            if not self._token_saved():
                return self._json({"ok": False, "error": FINISH_SETUP, "setup": True}, 400)
            return self._json(self.server.jobs.start("review", params))
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
        if r in ("api/prep", "api/team", "api/goals", "api/result", "api/series", "api/notes", "api/schedule",
                 "api/strategy", "api/intel-preview", "api/intel-import", "api/weekly", "api/discord-post",
                 "api/discord-check"):
            try:
                return getattr(self, "_post_" + r[4:].replace("-", "_"))(body)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                app.say("ERROR in " + r + ":\n" + traceback.format_exc())
                return self._json({"ok": False, "error": friendly(e)}, 500)
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

