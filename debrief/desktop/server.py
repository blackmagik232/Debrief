"""Local HTTP server for the Debrief desktop window.

Bound to 127.0.0.1 on a random free port. Every URL starts with a random per-session
secret, so other pages on this PC can't reach the API. POSTs also need a custom header
(which a cross-origin page can't send without a CORS preflight this server never answers).
"""
from __future__ import annotations

import json
import math
import mimetypes
import secrets
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
REVIEW_DIR_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
GOAL_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# race result fields: (kind, min, max). results.save validates again; this is the server's own gate.
RESULT_FIELDS = {"start_pos": (int, 1, 200), "finish_pos": (int, 1, 200), "field_size": (int, 1, 200),
                 "incidents": (int, 0, 999), "irating_before": (int, 0, 20000), "irating_after": (int, 0, 20000),
                 "sr_before": (float, 0.0, 10.0), "sr_after": (float, 0.0, 10.0), "sof": (int, 0, 20000),
                 "laps_led": (int, 0, 2000)}


def clean_result_fields(raw) -> dict:
    """Validate the race-result form. Empty boxes are left out. Raises ValueError with a friendly message."""
    if not isinstance(raw, dict):
        raise ValueError("Bad request.")
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
            num = float(str(v).strip().replace(",", "")) if isinstance(v, str) else float(v)
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
    if out.get("field_size") is not None:
        for k in ("start_pos", "finish_pos"):
            if k in out and out[k] > out["field_size"]:
                raise ValueError(f"{labels[k]} can't be more than the number of cars.")
    notes = raw.get("notes")
    if notes not in (None, ""):
        if not isinstance(notes, str):
            raise ValueError("Notes must be text.")
        if len(notes) > 500:
            raise ValueError("Notes can be up to 500 characters.")
        out["notes"] = notes.strip()
    return out


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


def friendly(e: BaseException) -> str:
    if isinstance(e, Garage61Error):
        m = str(e)
        if m.startswith("No Garage 61 token"):
            return "No Garage 61 token saved yet. Open Settings and paste your token."
        m = m.replace("Check settings.toml, or create", "Paste it again in Settings, or create")
        return m if m.startswith("Garage 61") else f"Garage 61: {m}"
    if isinstance(e, SystemExit):
        return str(e.code) if e.code not in (None, 0) else "Stopped."
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
              "sync": "Syncing your Garage 61 history"}


class Jobs:
    """One background job at a time: a review, a connection check, a prep page, a team refresh or a
    history sync. A sync gives way: starting anything else pauses it (it resumes on the next sync)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.st = {"state": "idle"}
        self.stop_sync = threading.Event()
        self.thread = None

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
            return json.loads(json.dumps(self.st))

    @property
    def running(self):
        with self.lock:
            return self.st.get("state") == "running"

    def start(self, kind: str, params: dict | None = None) -> dict:
        if kind not in JOB_TITLES:
            raise ValueError(kind)
        if kind != "sync" and self.kind == "sync":
            self.pause_sync()
        with self.lock:
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
            elif kind == "team":
                page = app.cmd_team(cfg, progress=cb)
                result = {"page": _lib_rel(cfg, page)}
            else:
                result = {"lines": app.cmd_check(cfg, progress=cb)}
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
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    @staticmethod
    def _summary(p: Path) -> dict:
        d = json.loads(p.read_text(encoding="utf-8"))
        m = d.get("meta", {})
        best = (d.get("best") or {}).get("t")
        return {"dir": p.parent.name, "track": m.get("track") or "Unknown track", "car": m.get("car") or "Unknown car",
                "started_at": m.get("started_at") or "", "date": library.local_day(m.get("started_at")) or p.parent.name[:10],
                "session": m.get("session") or "", "best": app.fmt_t(best) if best else "",
                "laps": len(d.get("laps", []))}

    def list(self, folder: Path) -> list:
        items = []
        with self.lock:
            for p in (folder / "reviews").glob("*/review.json"):
                try:
                    mt = p.stat().st_mtime
                    hit = self.cache.get(p)
                    if not hit or hit[0] != mt:
                        hit = (mt, self._summary(p))
                        self.cache[p] = hit
                    items.append(hit[1])
                except Exception:  # noqa: BLE001  (a half-written or old review: skip it)
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
        self.timeout_s = heartbeat_timeout
        self.last_ping = time.monotonic() + max(0.0, startup_grace - heartbeat_timeout)
        self.stopping = threading.Event()

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
            if idle > self.timeout_s and not self.jobs.running:
                app.say(f"Window closed (no heartbeat for {idle:.0f}s). Stopping.")
                self.stopping.set()
                threading.Thread(target=self.shutdown, daemon=True).start()
                return


class Handler(BaseHTTPRequestHandler):
    server: DebriefServer
    protocol_version = "HTTP/1.1"
    server_version = "Debrief"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the secret URL and request noise out of the log
        pass

    # ---------- helpers
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}

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
            return self._file(UI / "index.html")
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
        if r in ("api/combos", "api/teams", "api/goals", "api/result", "api/pages", "api/sessions"):
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
            teams = api.teams()
            used = api.requests_made
        out = None if teams is None else [{"slug": str(t.get("slug")), "name": str(t.get("name") or t.get("slug"))}
                                         for t in teams if isinstance(t, dict) and t.get("slug")]
        return self._json({"ok": True, "teams": out, "team_slug": cfg["team_slug"], "requests": used,
                           "token": api is not None})

    def _get_goals(self, q):
        cfg = self._cfg()
        goals = self._module("goals")
        folder = cfg["library_folder"]
        return self._json({"ok": True, "goals": goals.load(folder), "progress": goals.progress(folder)})

    def _get_result(self, q):
        cfg = self._cfg()
        d = self._review_dir(cfg, q.get("review"))
        results = self._module("results")
        return self._json({"ok": True, "review": d.name, "result": results.load(d)})

    def _get_sessions(self, q):
        """Your synced sessions (races, qualifying, practice), newest first. Never a request."""
        cfg = self._cfg()
        api = self._cache_api(cfg)
        if api is None:
            return self._json({"ok": True, "token": False, "summary": None, "sessions": []})
        from .. import sync
        store = sync.Store.for_api(api)
        dirs = sorted((p.parent.name for p in (Path(cfg["library_folder"]) / "reviews").glob("*/review.json")),
                      reverse=True)
        rows = []
        for s in store.sessions():
            ev = str(s.get("event") or "")
            name = None
            if ev:  # review folders end with the event's last 6 characters (+ _practice / _qualifying)
                end = "_" + ev[-6:].lower() + {1: "_practice", 2: "_qualifying"}.get(s.get("type"), "")
                name = next((d for d in dirs if d.endswith(end)), None)
            rows.append({k: s.get(k) for k in ("key", "event", "track_id", "car_id", "track", "car", "type", "session",
                                                 "start", "laps", "clean_laps", "best", "best_clean")} | {"review": name})
        return self._json({"ok": True, "token": True, "summary": store.summary(), "sessions": rows,
                           "sync_on_open": cfg.get("sync_on_open", True)})

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

    def _file(self, p: Path):
        try:
            body = p.read_bytes()
        except OSError:
            return self._nf()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
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
            if not idx.exists() or idx.stat().st_mtime < newest:
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
        if p.suffix.lower() not in LIB_TYPES or not p.is_file():
            return self._nf()
        self._file(p)

    # ---------- POST
    def do_POST(self):
        r = self._route()
        if r is None or self.headers.get("X-Debrief") != "1":
            return self._nf()
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
            return self._json(self.server.jobs.start("review", params))
        if r == "api/sync":
            return self._post_sync(body)
        if r == "api/sync-stop":
            if self.server.jobs.kind == "sync":
                self.server.jobs.stop_sync.set()
            return self._json({"ok": True})
        if r == "api/check":
            return self._json(self.server.jobs.start("check"))
        if r in ("api/prep", "api/team", "api/goals", "api/result"):
            try:
                return getattr(self, "_post_" + r[4:])(body)
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
                return self._json({"ok": False, "error": f"Couldn't write settings.toml ({e.strerror})."}, 500)
            app.say("Settings saved from the desktop app" + (" (token updated)." if "token" in body and body["token"] else "."))
            return self._json({"ok": True, "settings": view})
        if r == "api/open-folder":
            lib = self._library_folder()
            if lib is None:
                return self._json({"ok": False, "error": "Set up settings.toml first."}, 400)
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

