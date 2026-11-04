"""Debrief: review your last iRacing race from Garage 61 and file it in your library.

Usage:
    python -m debrief review          review your most recent race
    python -m debrief review --event EVENT_ID
    python -m debrief check           test the token and show what your account can access
    python -m debrief library         rebuild the library home page
    python -m debrief prep --track ID --car ID   build a track prep page for one track and car
    python -m debrief team            update the Midfield Menace team hub
    python -m debrief weekly          update this week's team leaderboards for your series
    python -m debrief sync            download your Garage 61 history (races and practice)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import brand, engine, report, library
from .engine import g
from .g61 import Garage61, Garage61Error

DEFAULT_TEAM = brand.GROUP_SLUG

# Two ways Debrief runs:
#  * from source (Setup.bat + .venv): everything lives in the Debrief folder next to the code, as always.
#  * installed (a PyInstaller build, sys.frozen is set): the program files are read-only, so the user's
#    data (settings.toml, .cache, debrief.log, .edge-profile, the .debrief-running lock) lives in
#    %LOCALAPPDATA%\Debrief (~/.local/share/Debrief off Windows), and bundled files come from sys._MEIPASS.
# HERE is always the data home, so existing code keeps working; RES is where bundled resources are.
FROZEN = bool(getattr(sys, "frozen", False))


def _frozen_data_home() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Debrief"
    return Path.home() / ".local" / "share" / "Debrief"


if FROZEN:
    HERE = _frozen_data_home()
    RES = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    try:
        HERE.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # reported later, when something actually needs to be written there
else:
    HERE = Path(__file__).resolve().parent.parent
    RES = HERE


def data_folder_label() -> str:
    """How messages should name the place where settings.toml and debrief.log live."""
    return f"Debrief's data folder ({HERE})" if FROZEN else "the Debrief folder"


def ensure_settings() -> bool:
    """Installed app only: create settings.toml from the example on first start, so the window opens
    straight into first-run setup. Returns True if a file was created. Never touches a source install."""
    if not FROZEN or os.environ.get("DEBRIEF_SETTINGS"):
        return False
    p = HERE / "settings.toml"
    if p.exists():
        return False
    try:
        text = (RES / "settings.example.toml").read_text(encoding="utf-8-sig")
    except OSError:
        text = 'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = "~/Documents/Debrief Reviews"\n'
    text = text.replace("Copy this file to settings.toml and fill in your token.",
                        "Debrief created this file. Paste your token in Debrief's window, not here.", 1)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- settings
def documents_dir() -> Path:
    """The real Documents folder (handles OneDrive redirection on Windows)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]
            u = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            gid = GUID(u.fields[0], u.fields[1], u.fields[2], (wintypes.BYTE * 8).from_buffer_copy(u.bytes[8:]))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(gid), 0, None, ctypes.byref(ptr)) == 0:
                path = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return path
        except Exception:
            pass
    return Path.home() / "Documents"


def _parse_toml(txt: str) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # installed by requirements.txt on 3.10
        except ModuleNotFoundError:
            if FROZEN:
                raise SystemExit("This copy of Debrief is missing a part (the settings reader). "
                                 "Please install Debrief again.")
            raise SystemExit("Please run Setup.bat again (a settings reader is missing).")
    try:
        return tomllib.loads(txt)
    except tomllib.TOMLDecodeError as e:
        where = f" (it's in {data_folder_label()})" if FROZEN else ""
        raise SystemExit(f"settings.toml has a typo{where} ({e}).\n"
                         "Tip: put Windows paths in single quotes, like  library_folder = 'C:\\Users\\you\\Documents\\Debrief Reviews'")


def settings_path() -> Path:
    return Path(os.environ.get("DEBRIEF_SETTINGS", HERE / "settings.toml"))


def normalise_salutation(v) -> str:
    """settings salutation: a string, stripped, at most 20 characters; anything else is ""."""
    if v is None or isinstance(v, (bool, dict, list)):
        return ""
    return str(v).strip()[:20]


def load_settings() -> dict:
    p = settings_path()
    if not p.exists() and ensure_settings():
        p = settings_path()
    if not p.exists():
        if FROZEN:
            raise SystemExit("Debrief's settings are missing and couldn't be created. Close Debrief and open "
                             "it again: the welcome screen sets them up.")
        raise SystemExit("Debrief's settings are missing. Close Debrief and open it again: the welcome screen "
                         "sets it up (or open the Settings page).")
    cfg = _parse_toml(p.read_text(encoding="utf-8-sig"))
    lib = cfg.get("library_folder") or ""
    if not lib or lib.replace("\\", "/").rstrip("/") in ("~/Documents/Debrief Reviews",):
        lib = str(documents_dir() / "Debrief Reviews")
    cfg["library_folder"] = os.path.expandvars(os.path.expanduser(lib))
    cfg["max_reference_laps"] = max(0, min(3, int(cfg.get("max_reference_laps", 3))))
    cfg["include_practice"] = bool(cfg.get("include_practice", False))
    cfg["salutation"] = normalise_salutation(cfg.get("salutation", ""))
    cfg["team_slug"] = str(cfg.get("team_slug") or DEFAULT_TEAM).strip().lower() or DEFAULT_TEAM
    # 2.21: the team (a name and its members) is not the Garage 61 group the laps come from (team_slug above)
    cfg["team_name"] = " ".join(str(cfg.get("team_name") or brand.TEAM_NAME).split())[:60] or brand.TEAM_NAME
    cfg["team_members"] = brand.parse_members(cfg.get("team_members"))
    # history sync: catch up with Garage 61 each time the window opens; telemetry "best" or "none"
    cfg["sync_on_open"] = bool(cfg.get("sync_on_open", True))
    cfg["check_updates"] = bool(cfg.get("check_updates", True))
    cfg["sync_telemetry"] = "none" if str(cfg.get("sync_telemetry", "best")).strip().lower() == "none" else "best"
    # optional iRacing data API credentials: only stored here; nothing runs with them by default
    for k in ("iracing_client_id", "iracing_client_secret"):
        cfg[k] = str(cfg.get(k) or "").strip()
    # 2.4 shared team track cards: a folder the team syncs (OneDrive, Google Drive, Dropbox...), and whether
    # your own cards are written there. Reading teammates' cards needs only the folder.
    tcf = str(cfg.get("team_cards_folder") or "").strip()
    cfg["team_cards_folder"] = os.path.expandvars(os.path.expanduser(tcf)) if tcf else ""
    cfg["share_my_cards"] = cfg.get("share_my_cards") is True
    # cache + log live in HERE: next to the code from source (Store-installed Python redirects AppData
    # writes), or in %LOCALAPPDATA%\Debrief for the installed app.
    # DEBRIEF_CACHE_DIR lets the tests use a throwaway cache.
    cfg["cache_dir"] = os.environ.get("DEBRIEF_CACHE_DIR") or str(HERE / ".cache")
    return cfg


def make_api(cfg):
    kw = {}
    if cfg.get("api_base"):
        kw["base_url"] = cfg["api_base"]
    return Garage61(cfg.get("token", ""), Path(cfg["cache_dir"]), log=say, **kw)


LOG_FILE = None
LOG_MAX_BYTES = 1_000_000   # debrief.log is moved to debrief.log.1 at about 1 MB ...
LOG_BACKUPS = 2             # ... and debrief.log.1 to .2, so at most ~3 MB of log in all
_LOG_LOCK = threading.Lock()


def _home_forms() -> list[str]:
    """The user's home folder as it can appear in a log line (both slash styles), longest first."""
    forms = set()
    for h in (os.environ.get("USERPROFILE"), os.environ.get("HOME"), str(Path.home())):
        h = (h or "").rstrip("\\/")
        if len(h) > 3:   # never "C:" or "/"
            forms.update({h, h.replace("\\", "/"), h.replace("/", "\\")})
    return sorted(forms, key=len, reverse=True)


# 2.7.1 (M5): one scrubber for everything that may be shared (debrief.log via say(), Copy diagnostics).
_SEP = r"(?:\\+|/+|%5[Cc]|%2[Ff])+"         # \, \\ (repr / JSON), /, and their URL-encoded forms
_HOME_END = r"(?![A-Za-z0-9_~])"
_KNOWN_SECRETS: set = set()                  # tokens / webhooks this process has seen (saved, or replaced)


def remember_secret(value) -> None:
    """Note a secret (a token or webhook, the current one or one just replaced) so log excerpts never show it."""
    v = str(value or "").strip()
    if len(v) >= 8 and not v.startswith("PASTE"):
        _KNOWN_SECRETS.add(v)


def _path_rx(parts: list) -> str:
    """A regex for a path given as its segments: any slash style (\\, \\\\, /, %5C, %2F), a space as itself,
    %20 or +."""
    import re as _re
    return _SEP.join(_re.escape(p).replace(r"\ ", " ").replace(" ", r"(?: |%20|\+)") for p in parts)


def _home_patterns() -> list:
    """(compiled regex, replacement) for the home folder in every form a log line can carry it: both slash
    styles, doubled backslashes (a repr or JSON), URL-encoded (file:///C:/Users/Swazi%20Williams), and the
    8.3 short name of the user's folder (C:\\Users\\SWAZIW~1)."""
    import re as _re
    pats, seen = [], set()
    for h in _home_forms():
        parts = [p for p in _re.split(r"[\\/]+", h) if p]
        key = tuple(x.lower() for x in parts)
        if len(parts) < 1 or key in seen:
            continue
        seen.add(key)
        drive = bool(_re.match(r"^[A-Za-z]:$", parts[0]))
        lead = "" if drive else _SEP      # /home/x: the leading slash is part of the match
        flags = _re.IGNORECASE if os.name == "nt" or drive else 0
        pats.append((_re.compile(lead + _path_rx(parts) + _HOME_END, flags), "~"))
        if len(parts) >= 2:                # the user's folder as an 8.3 short name: <parent>\SWAZIW~1
            pats.append((_re.compile(lead + _path_rx(parts[:-1]) + _SEP + r"[^\\/\s\"'<>|:*?]{1,8}~\d+" + _HOME_END,
                                     flags), "~"))
    return pats


# user names that are also everyday racing words: masking them would wreck the log, and they identify no one
_COMMON_NAMES = {"race", "racer", "user", "admin", "owner", "home", "spa", "max", "lap", "fast", "speed", "team",
                 "test", "guest", "driver", "sim", "pc", "gamer", "monza", "lime", "rock"}


def _user_names() -> list:
    """The Windows user name and the last segment of the home folder (3+ characters), for the backstop."""
    import re as _re
    names = set()
    for v in [os.environ.get("USERNAME"), os.environ.get("USER")] + \
             [(_re.split(r"[\\/]+", h.rstrip("\\/")) or [""])[-1] for h in _home_forms()]:
        v = (v or "").strip()
        if len(v) >= 4 and v.lower() not in _COMMON_NAMES and not _re.match(r"^[A-Za-z]:$", v):
            names.add(v)
    return sorted(names, key=len, reverse=True)


def scrub_home(text: str) -> str:
    """Write "~" instead of the home folder (it holds the Windows user name), so a log can be shared."""
    for rx, rep in _home_patterns():
        text = rx.sub(rep, text)
    return text


_TOKEN_SHAPES = None


def _token_shapes():
    global _TOKEN_SHAPES
    if _TOKEN_SHAPES is None:
        import re as _re
        val = r"[A-Za-z0-9._~+/=%-]{6,}"
        _TOKEN_SHAPES = [
            # Authorization: Basic|Bearer|Token <value>  (the scheme is kept, the credentials go)
            (_re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer|token|digest)?\s*)" + val), r"\1…"),
            (_re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1…"),
            # token=..., access_token=..., "api_key": "...", password: ...  (no word boundary needed)
            (_re.compile(r"(?i)((?<![a-z])(?:[a-z]+_)?(?:token|secret|key|apikey|auth|password|passwd)[\"']?\s*[:=]\s*[\"']?)" + val),
             r"\1…"),
            # "token <long value with a digit>" (a library's own error message quoting it)
            (_re.compile(r"(?i)((?:token|secret|password)\s+)(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,}"), r"\1…"),
            (_re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+"), r"\1…"),
            (_re.compile(r"[A-Za-z0-9._%+-]+@(?!\d+x\.)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), "[email]"),
        ]
    return _TOKEN_SHAPES


def scrub_text(text: str, secrets=(), names: bool = True) -> str:
    """Everything in `text` that could identify or let someone in, taken out before it is shared: the home
    folder in every form (scrub_home), the given secrets and every one this process has seen (remember_secret),
    token / key / password / Authorization shapes, Discord webhook tokens and e-mail addresses. names=True
    also replaces the bare Windows user name (whole word, any case) with [user], as a backstop."""
    import re as _re
    text = scrub_home(str(text))
    for sec in sorted({str(x or "").strip() for x in list(secrets) + list(_KNOWN_SECRETS)}, key=len, reverse=True):
        if len(sec) >= 8:
            text = _re.sub(_re.escape(sec), "…", text, flags=_re.IGNORECASE)
            tail = sec.rstrip("/").rsplit("/", 1)[-1]   # a webhook's token on its own
            if tail != sec and len(tail) >= 20:
                text = text.replace(tail, "…")
    for rx, rep in _token_shapes():
        text = rx.sub(rep, text)
    if names:
        for n in _user_names():
            text = _re.sub(r"(?<![A-Za-z0-9])" + _path_rx([n]) + r"(?![A-Za-z0-9])", "[user]", text,
                           flags=_re.IGNORECASE)
    return text


def _rotate_log(path: str) -> None:
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(LOG_BACKUPS, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{i}")
    except OSError:
        pass   # another Debrief has the file open (Windows): keep writing, rotate next time


def say(msg):
    if sys.stdout is not None:  # None under pythonw.exe (desktop app)
        try:
            print(msg, flush=True)
        except (OSError, ValueError):
            pass
    if LOG_FILE:
        line = scrub_text(str(msg), names=False) + "\n"
        with _LOG_LOCK:
            _rotate_log(LOG_FILE)
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


# ---------------------------------------------------------------- helpers
def _id(v):
    return g(v, "id") if isinstance(v, dict) else v


def lap_time(l):
    t = g(l, "lap_time", "lapTime")
    try:
        return float(t) if t else None
    except (TypeError, ValueError):
        return None


def lap_start_dt(l):
    s = g(l, "start_time", "startTime")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_clean(l):
    v = g(l, "clean", "isClean")
    if v is None and g(l, "unclean") is not None:
        return not bool(g(l, "unclean"))
    return True if v is None else bool(v)


_PARTIAL_FLAGS = ("incomplete", "missing")
_PIT_FLAGS = ("pitIn", "pitOut", "pitlane", "pit_in", "pit_out", "pit_lane")


def is_full_lap(l) -> bool:
    """Not marked partial (incomplete/missing) or as a pit lap by Garage 61."""
    return not any(g(l, k) for k in _PARTIAL_FLAGS + _PIT_FLAGS)


def full_lap_best(laps):
    """Your best lap time from a list of Garage 61 laps that can stand as "your best": a full, clean lap
    no shorter than 75% of the median full lap (so a 7 s "lap" at the finish line never counts).
    Falls back to flagged full laps (still plausible) when every lap was flagged; None if nothing fits."""
    full = [l for l in laps if lap_time(l) and is_full_lap(l)]
    ts = [lap_time(l) for l in full]
    if not ts:
        return None
    med = st.median(ts)
    ok = [l for l in full if 0.75 * med <= lap_time(l) <= 1.25 * med]
    clean = [lap_time(l) for l in ok if is_clean(l)]
    if clean:
        return min(clean)
    return min((lap_time(l) for l in ok), default=None)


def session_type(l):
    v = g(l, "session_type", "sessionType")
    if isinstance(v, dict):
        v = g(v, "type", "id")
    if isinstance(v, str):
        v = {"practice": 1, "qualify": 2, "qualifying": 2, "race": 3}.get(v.lower(), v)
    return v


# Garage 61's car and track lists are downloaded again (refresh=True) at most once each per run, however many
# unknown ids a review, a page or the on-track list meets. Keyed by the client, so a new run (a new client)
# may refresh again; reset_refreshed() clears it. Weak keys: a finished run's client isn't kept alive.
import weakref as _weakref
_REFRESHED = _weakref.WeakKeyDictionary()


def reset_refreshed() -> None:
    _REFRESHED.clear()


def _was_refreshed(api, kind) -> bool:
    try:
        return kind in _REFRESHED.get(api, ())
    except TypeError:   # an object that can't be weakly referenced: no memory, as before
        return False


def _list_for(api, kind, refresh=False):
    if refresh:
        if _was_refreshed(api, kind):
            refresh = False
        else:
            try:
                _REFRESHED.setdefault(api, set()).add(kind)
            except TypeError:
                pass
    return api.cars(refresh) if kind == "car" else api.tracks(refresh)


def name_of(api, kind, value):
    """Name for a car/track id; uses the object's own name if the lap already includes it. An id missing
    from the saved list downloads the list again, at most once per kind per run."""
    if isinstance(value, dict) and g(value, "name"):
        return g(value, "name") + ((" (" + g(value, "variant") + ")") if g(value, "variant") else "")
    vid = _id(value)
    for refresh in (False, True):
        if refresh and _was_refreshed(api, kind):
            break   # already downloaded again this run: it isn't there
        items = _list_for(api, kind, refresh)
        for it in items:
            if g(it, "id") == vid:
                return g(it, "name", default=str(vid)) + ((" (" + g(it, "variant") + ")") if g(it, "variant") else "")
    return f"{kind} {vid}"


def track_length(api, value):
    if isinstance(value, dict):
        tl = g(value, "lap_length", "lapLength", "length")
        if tl:
            return tl
    for it in api.tracks():
        if g(it, "id") == _id(value):
            return g(it, "lap_length", "lapLength", "length")
    return None


def fmt_t(s):
    return engine.fmt_t(s)


# ---------------------------------------------------------------- comparison laps
def _depth_context(laps):
    try:
        from . import depth
        return depth.context_of(laps)
    except Exception:  # noqa: BLE001  (comparison laps still work on pace alone)
        return None


def pick_refs(api: Garage61, car, track, my_best: float, my_slug: str, limit: int, me=None):
    """Comparison laps from teammates and followed drivers only (all the API allows). me: your laps' conditions
    and session type (depth.context_of), which the main comparison is matched on when given."""
    if limit <= 0:
        return []
    teams = [g(t, "slug", "id") for t in api.teams() if g(t, "slug", "id")]
    params = dict(cars=[car], tracks=[track], drivers=["following"], group="driver", age=-2, limit=200)
    if teams:
        params["teams"] = teams
    try:
        cands = api.find_laps(**params)
    except Garage61Error as e:
        say(f"  Couldn't load comparison laps: {e}")
        return []
    from . import team   # lazy: team imports app
    cands = [c for c in cands if g(g(c, "driver", default={}) or {}, "slug") != my_slug
             and lap_time(c) and is_clean(c) and team.usable(c)]
    # a driver's "best" can be a partial lap (e.g. 6 s at the line): judge every candidate against the
    # typical full lap (their laps plus your best), the same way the team and prep pages do
    anchor = team.anchor_for([lap_time(c) for c in cands], my_best)
    cands = [c for c in cands if team.plausible(lap_time(c), anchor)]
    if not cands:
        return []
    cands.sort(key=lap_time)
    chosen = []

    def role_for(c, base):
        d = lap_time(c) - my_best
        rel = f"{abs(d):.2f}s {'quicker' if d < 0 else 'slower'} than your best"
        return f"{base} ({rel})"

    def add(c, base, main=False):
        if c is None or any(x is c for x in chosen):
            return
        c["_role"] = role_for(c, base)
        c["_main"] = main
        chosen.append(c)

    quicker = [c for c in cands if lap_time(c) < my_best]
    from . import depth   # lazy: only this lookup needs it
    main = depth.pick_main(quicker, my_best, me, lap_time) if quicker else cands[0]
    add(main, "Main comparison", main=True)
    add(cands[0], "Fastest teammate or followed driver")
    add(min(cands, key=lambda c: abs(lap_time(c) - (my_best - 0.3))), "Close to your pace")
    return chosen[:limit]


def on_track(api: Garage61, track, event_id, my_slug: str, my_car, classes=None) -> dict:
    """Everyone Garage 61 lets you see (teammates and followed drivers) who drove in the same event, in any
    car. One request. classes(car_name) -> class name or None comes from the season schedule when one is
    loaded, so a different car can be told apart from a different class."""
    track = _id(track)   # Garage 61 wants the id, never the track object
    teams = [g(t, "slug", "id") for t in api.teams() if g(t, "slug", "id")]
    params = dict(tracks=[track], event=event_id, drivers=["following"], group="driver", unclean=True, limit=200)
    if teams:
        params["teams"] = teams
    try:
        laps = api.find_laps(**params)
    except Garage61Error as e:
        say(f"  Couldn't load who else was on track: {e}")
        return {"drivers": [], "multiclass": False, "error": True}
    my_car_name = name_of(api, "car", my_car) if my_car is not None else ""
    my_class = classes(my_car_name) if classes else None
    by_driver = {}
    names = {}

    def car_name_of(car):
        # unknown car ids: name_of refreshes Garage 61's car list at most once per run
        if car not in names:
            names[car] = name_of(api, "car", car)
        return names[car]
    for l in laps:
        drv = g(l, "driver", default={}) or {}
        slug = g(drv, "slug")
        if not slug or slug == my_slug or str(g(l, "event")) != str(event_id):
            continue
        t = lap_time(l)
        car = _id(g(l, "car_id", "carId", "car"))
        key = (slug, car)
        cur = by_driver.get(key)
        if cur is None:
            name = " ".join(filter(None, [g(drv, "firstName", "firstname"), g(drv, "lastName", "lastname")])) or slug
            car_name = car_name_of(car) if car is not None else ""
            cls = classes(car_name) if classes else None
            cur = by_driver[key] = {"name": name, "car": car_name, "car_id": car, "best": None, "lap_id": None,
                                    "same_car": car == _id(my_car),
                                    "same_class": None if (cls is None or my_class is None) else cls == my_class,
                                    "class": cls}
        if t and is_clean(l) and is_full_lap(l) and (cur["best"] is None or t < cur["best"]):
            cur["best"], cur["lap_id"] = round(t, 3), str(g(l, "id"))
    drivers = sorted(by_driver.values(), key=lambda d: (d["best"] is None, d["best"] or 0))
    return {"drivers": drivers, "multiclass": any(d["same_class"] is False for d in drivers)}


# ---------------------------------------------------------------- commands
def recent_combos(api, days=30, cache_dir=None):
    """(track, car) pairs you've driven recently, newest first. Garage 61's lap search needs a track,
    so we ask the driving statistics which tracks to look at."""
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = api.my_statistics(start)
    if cache_dir and rows:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        (Path(cache_dir) / "sample_statistics.json").write_text(json.dumps(rows[:3], indent=2), encoding="utf-8")
    seen, out = set(), []
    def day(r):
        return str(g(r, "day", "date", "startDate", "start", default=""))
    for r in sorted(rows, key=day, reverse=True):
        t = _id(g(r, "track", "trackId", "track_id"))
        c = _id(g(r, "car", "carId", "car_id"))
        if t is None or (t, c) in seen:
            continue
        seen.add((t, c))
        out.append((t, c))
    return out


def laps_for(api, track, car, fresh=True, **params):
    q = dict(tracks=[track], drivers=["me"], group="none", **params)
    if car is not None:
        q["cars"] = [car]
    return api.find_laps(fresh=fresh, **q)


NO_RACE_MSG = ("Debrief couldn't find a race from the last 30 days. If you just finished, wait a few minutes for "
               "Garage 61 to upload it and try again. Older race? Pick it on the Sessions page.")
PRACTICE_OFF_MSG = "Practice sessions are off: turn on 'Include practice' in Settings > Review options."
STORE_FRESH_S = 600   # a sync this recent is trusted for "your newest race" (no requests)


def _no_race_msg(sessions) -> str:
    return NO_RACE_MSG + ("" if 1 in (sessions or []) else " " + PRACTICE_OFF_MSG)


_STORE_PICK: dict = {}   # the synced session last_event_from_store picked (for the progress message)


def _store_pick_text() -> str:
    s = _STORE_PICK
    kind = {1: "practice", 2: "qualifying", 3: "race"}.get(s.get("type"), "session")
    when = ""
    try:
        t = datetime.fromisoformat(str(s.get("start")).replace("Z", "+00:00")).astimezone()
        when = t.strftime(" on %a %d %b at %H:%M")
    except (TypeError, ValueError):
        pass
    where = f" at {s['track']}" if s.get("track") else ""
    return f"your {kind}{where}{when}"


def _already_reviewed(library_folder, event, stype) -> bool:
    """A review folder for this event exists (folders end with "_" + the event's last 6 characters, plus
    _practice / _qualifying)."""
    if not library_folder or not event:
        return False
    end = "_" + str(event)[-6:].lower() + {1: "_practice", 2: "_qualifying"}.get(stype, "")
    try:
        return any(d.name.lower().endswith(end) for d in (Path(library_folder) / "reviews").iterdir()
                   if (d / "review.json").is_file())
    except OSError:
        return False


def last_event_from_store(api, sessions, max_age_s=STORE_FRESH_S, now=None, library_folder=None):
    """(event, track_id, car_id) of the newest synced session of the wanted types when the local history
    sync finished under max_age_s ago, else None. Reads files only: never a request.
    None too when that session already has a review: the race you just finished may have reached Garage 61
    after the sync, so the search runs instead of quietly reviewing the previous race again."""
    try:
        from . import sync
        store = sync.Store.for_api(api)
        stt = store.state()
        if not stt.get("first_complete") or stt.get("stopped"):
            return None
        last = sync._parse(stt.get("last_success"))
        now = now or datetime.now(timezone.utc)
        if last is None or not (0 <= (now - last).total_seconds() < max_age_s):
            return None
        want = set(sessions or [3])
        for s in store.sessions():   # newest first
            if s.get("event") and s.get("type") in want and s.get("track_id") is not None:
                started = sync._parse(s.get("start"))
                if started is not None and (now - started).days > 30:
                    return None   # same 30-day window as the search: the search then says so
                if _already_reviewed(library_folder, s["event"], s.get("type")):
                    return None
                _STORE_PICK.clear()
                _STORE_PICK.update(s)
                return s["event"], s["track_id"], s.get("car_id")
    except Exception:  # noqa: BLE001  (an odd store only means the normal search runs)
        import traceback
        say("  Couldn't read the synced sessions; searching Garage 61 instead:\n" + traceback.format_exc())
    return None


def find_last_event(api, sessions, cache_dir=None, library_folder=None):
    """Newest race across the tracks/cars you drove recently. A history sync from the last 10 minutes
    answers with no requests; otherwise the driving statistics plus up to 6 lap searches."""
    hit = last_event_from_store(api, sessions, library_folder=library_folder)
    if hit is not None:
        say(f"  Reviewing {_store_pick_text()}, the newest in your synced history (no Garage 61 requests). "
            "Just finished a race that isn't this one? Give Garage 61 a few minutes, then sync and try again.")
        return hit
    combos = recent_combos(api, 30, cache_dir)
    if not combos:
        raise SystemExit(_no_race_msg(sessions))
    best = None
    for t, c in combos[:6]:  # most recent few combinations is plenty
        laps = laps_for(api, t, c, sessionTypes=sessions, age=30, limit=1000)
        dated = [l for l in laps if lap_start_dt(l) and g(l, "event")]
        if dated:
            newest = max(dated, key=lap_start_dt)
            if best is None or lap_start_dt(newest) > lap_start_dt(best):
                best = newest
    if best is None:
        raise SystemExit(_no_race_msg(sessions))
    return g(best, "event"), _id(g(best, "track_id", "trackId", "track")), _id(g(best, "car_id", "carId", "car"))


def session_label(kind) -> str:
    """meta.session for a review of laps of this session type: Practice / Qualifying, else Race (a race
    session or an unknown type both count as a race, as library.session_kind classifies them)."""
    return {1: "Practice", 2: "Qualifying"}.get(kind, "Race")


def _one_session(laps):
    """(laps, kind): the laps of ONE session. An event searched with practice included can return its
    practice, qualifying and race laps together; the review is of the race when the event has one, else of
    the session of the newest lap. kind is 1 / 2 / 3, or None when the laps don't say (counted as a race)."""
    kinds = {session_type(l) for l in laps}
    if len(kinds) <= 1:
        return laps, next(iter(kinds), None)
    timed = lambda k: sum(1 for l in laps if session_type(l) == k and (lap_time(l) or 0) > 0)
    if 3 in kinds and timed(3) >= 2:
        pick = 3   # the race, when it has laps enough to review (a lap-1 crash falls back below)
    else:
        # the newest session with at least 2 timed laps (a race cut short on lap 1 is skipped); when none has
        # two, the newest session anyway
        enough = [l for l in laps if timed(session_type(l)) >= 2] or laps
        dated = [l for l in enough if lap_start_dt(l)]
        pick = session_type(max(dated, key=lap_start_dt)) if dated else session_type(enough[-1])
    return [l for l in laps if session_type(l) == pick], pick


TEL_FAIL_RUN = 3   # telemetry downloads failing in a row before the review stops asking (as sync.TEL_FAIL_RUN)
TEL_GIVING_UP = "Garage 61 isn't sending telemetry right now; reviewing with the laps already downloaded"
TEL_NONE_MSG = ("Garage 61 isn't sending telemetry right now, and none of this race's laps had been downloaded "
                "yet. Wait a few minutes and try the review again, or run Check connection (Settings).")
BROKEN_SPEED_MSG = ("One lap's telemetry looks broken (no speed data). Try the review again later, or review a "
                    "different session.")


def _stepper(progress):
    """say() plus an optional progress(fraction or None, message) callback for the desktop app."""
    def step(msg, frac=None):
        say(msg)
        if progress:
            try:
                progress(frac, str(msg))
            except Exception:
                pass
    return step


def _racecap_race(cfg, api, event_id, track_name, laps, my_name):
    """The race folder in RaceCap's library for this review, or None. Only with a folder in Settings; never fatal."""
    folder = str(cfg.get("racecap_library") or "").strip()
    try:
        from . import racecap_link, replay_scan
        own = replay_scan.find_for_event(cfg["library_folder"], event_id)
        if own is not None:     # a scan Debrief made itself for this event: no requests, no RaceCap needed
            scan = racecap_link.read_scan(own)
            car = racecap_link.find_me(scan["cars"], my_name) if scan else None
            if car is not None:
                return {"dir": own, "subsession": str(scan.get("subsession")), "scan": scan, "car": car, "official": None}
        if not folder:
            return None
        starts = [dt for dt in (lap_start_dt(l) for l in laps) if dt]
        found, why = racecap_link.prepare(folder, event_id, track_name, min(starts).isoformat() if starts else "", my_name,
                                          api, log=say)
        if found is None:
            say(f"  RaceCap link: {why}")
        return found
    except Exception:  # noqa: BLE001
        import traceback
        say("  Couldn't check RaceCap:\n" + traceback.format_exc())
        return None


def saved_review_dir(library_folder, event, stype):
    """2.18: the saved review folder of this event and session type (1 practice, 2 qualifying, 3 or None race),
    or None. Same folder-name rule as _already_reviewed; the newest such folder when there are several."""
    if not library_folder or not event:
        return None
    end = "_" + str(event)[-6:].lower() + {1: "_practice", 2: "_qualifying"}.get(stype, "")
    want = session_label(stype if stype in (1, 2) else 3)
    hits = []
    try:
        for d in (Path(library_folder) / "reviews").iterdir():
            if not d.name.lower().endswith(end):
                continue
            try:   # only a review of this session as such: an old mixed "Session" review is made again (2.7.2)
                m = json.loads((d / "review.json").read_text(encoding="utf-8")).get("meta") or {}
            except (OSError, ValueError, AttributeError):
                continue
            if str(m.get("event_id") or "") == str(event) and m.get("session") == want:
                hits.append(d)
    except OSError:
        return None
    return sorted(hits, key=lambda d: d.name)[-1] if hits else None


def _fmt_or(t):
    return fmt_t(t) if isinstance(t, (int, float)) and t > 0 else "none"


def rebuild_changes(old, new) -> list:
    """2.18: what a rebuild changed, in plain sentences, comparing the review saved before (old review.json) with
    the new one. Pure. ["Nothing changed: ..."] when the numbers a driver sees are the same."""
    if not isinstance(old, dict):
        return ["This review had no saved data before, so it was built fresh."]
    out = []

    def laps(d):
        return [l for l in (d.get("laps") or []) if isinstance(l, dict)]

    def analysed(d):
        c = d.get("counts") if isinstance(d.get("counts"), dict) else {}
        v = c.get("analysed")
        return v if isinstance(v, int) else len(laps(d))

    def best(d):
        b = d.get("best") if isinstance(d.get("best"), dict) else {}
        v = b.get("t")
        return float(v) if isinstance(v, (int, float)) else None

    def refs(d):
        return len([r for r in (d.get("refs") or []) if isinstance(r, dict)])

    for name, fn, unit in (("Laps", lambda d: len(laps(d)), "lap"), ("Laps with telemetry", analysed, "lap"),
                           ("Comparison laps", refs, "lap")):
        a, b = fn(old), fn(new)
        if a != b:
            out.append(f"{name}: {a} before, {b} now.")
    a, b = best(old), best(new)
    if (a is None) != (b is None) or (a is not None and abs(a - b) >= 0.0005):
        out.append(f"Best lap: {_fmt_or(a)} before, {_fmt_or(b)} now.")
    ra = (old.get("result") or {}) if isinstance(old.get("result"), dict) else {}
    rb = (new.get("result") or {}) if isinstance(new.get("result"), dict) else {}
    if ra.get("finish_pos") != rb.get("finish_pos") and rb.get("finish_pos") is not None:
        out.append(f"Finishing place: P{rb['finish_pos']} now.")
    if not out:
        out.append("Nothing changed: the same laps, best lap and comparison laps as the saved review.")
    return out


def cmd_review(cfg, event_id=None, open_after=True, progress=None, session_kind=None, track_id=None, car_id=None,
               rebuild=False, info=None):
    """Review your newest race, or (from the Sessions page) one synced session: event_id plus its
    session_kind (1 practice, 2 qualifying, 3 race) and track/car, so no search is needed.
    2.18: when that session already has a saved review and rebuild is False, nothing is downloaded: the saved
    page comes back and info (a dict, when given) gets {"saved": True}. With rebuild=True the review is made
    again and info["changes"] says what changed (rebuild_changes)."""
    info = info if isinstance(info, dict) else {}
    step = _stepper(progress)
    step("Connecting to Garage 61...", 0.02)
    api = make_api(cfg)
    me = api.me() or {}
    slug = g(me, "slug", default="")
    my_name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])) or slug
    step(f"Connected to Garage 61 as {my_name}.", 0.06)
    sessions = [1, 2, 3] if cfg["include_practice"] else [3]
    if session_kind in (1, 2, 3):
        sessions = [session_kind]
    ev_track, ev_car = track_id, car_id
    if not event_id:
        step("Finding your most recent race...", 0.08)
        event_id, ev_track, ev_car = find_last_event(api, sessions, cfg["cache_dir"], cfg["library_folder"])
    if not rebuild:
        for k in sorted(sessions, key=lambda k: k != 3):
            d = saved_review_dir(cfg.get("library_folder"), event_id, k if k in (1, 2) else 3)
            if d is not None:
                info["saved"] = True
                step("Already reviewed: opening the saved review. Nothing was downloaded.", 1.0)
                page = d / "index.html"
                if open_after and page.is_file():
                    webbrowser.open(page.as_uri())
                return page
    step(f"Loading event {event_id}...", 0.18)
    if ev_track is not None:
        all_laps = laps_for(api, ev_track, ev_car, event=event_id, unclean=True, limit=1000)
    else:
        all_laps = []
        for t, c in recent_combos(api, 60):
            all_laps = laps_for(api, t, c, event=event_id, unclean=True, limit=1000)
            if all_laps:
                break
    laps = [l for l in all_laps if session_type(l) in sessions]
    if not laps:
        keys = sorted(all_laps[0].keys()) if all_laps else []
        if keys:
            say(f"  Lap fields seen: {', '.join(keys)}")
        raise SystemExit("That session has no laps for you." if session_kind else "That event has no race laps for you.")
    laps, chosen_kind = _one_session(laps)
    car, track = g(laps[0], "car_id", "carId", "car"), g(laps[0], "track_id", "trackId", "track")
    car_name, track_name = name_of(api, "car", car), name_of(api, "track", track)
    timed = [l for l in laps if lap_time(l)]
    med = st.median(lap_time(l) for l in timed) if timed else 0
    # skip telemetry for laps the analysis will drop anyway (pit/tow/spin laps)
    wanted = [l for l in timed if lap_time(l) < med * 1.25]
    step(f"{len(laps)} laps found. Downloading telemetry for {len(wanted)} (already-saved laps are skipped)...", 0.24)
    tel, refused, skipped = {}, 0, 0
    run_fail = 0   # 2.7 (M2): Garage61Errors in a row from telemetry downloads; TEL_FAIL_RUN stops asking
    for i, l in enumerate(wanted):
        if progress:
            progress(0.24 + 0.46 * i / max(1, len(wanted)), f"Telemetry for lap {i + 1} of {len(wanted)}...")
        try:
            csv = api.lap_csv(g(l, "id"))
        except Garage61Error as e:
            if getattr(e, "fatal", False) or getattr(e, "status", None) == 401:
                raise   # the token was rejected: nothing else will work
            # 2.7.1 (M3): "can't reach Garage 61" (status None) counts toward the breaker like any other
            # failure, so a dropped connection mid-review finishes with the laps already downloaded
            run_fail += 1
            skipped += 1
            say(f"  Telemetry for lap {g(l, 'id')} failed: {e}")
            if run_fail >= TEL_FAIL_RUN:
                skipped += len(wanted) - i - 1
                if tel:
                    step(TEL_GIVING_UP)
                    break
                if getattr(e, "status", None) is None:
                    raise   # offline with nothing downloaded: the plain "can't reach Garage 61" message
                raise SystemExit(TEL_NONE_MSG)
            continue
        run_fail = 0
        if csv:
            tel[g(l, "id")] = csv
        else:
            refused += 1
    if refused or skipped:
        step(f"  {len(tel)} lap(s) with telemetry" +
             (f", {refused} with none available" if refused else "") +
             (f", {skipped} skipped (Garage 61 errors)" if skipped else "") + ".")
    if len(tel) < 2:
        raise SystemExit("Garage 61 didn't return telemetry for this race. Run Check connection (Settings) to see what your account can access.")
    with_tel = [l for l in laps if g(l, "id") in tel]
    # never a partial lap (the 7 s "lap" at the finish line) or a pit lap: that would pick nonsense refs
    my_best = full_lap_best(with_tel) or min(lap_time(l) for l in with_tel)
    step("Looking for comparison laps from your teammates and followed drivers...", 0.72)
    refs, ref_tel = [], {}
    try:
        refs = pick_refs(api, _id(car), _id(track), my_best, slug, cfg["max_reference_laps"], me=_depth_context(with_tel))
        for r in refs:
            csv = api.lap_csv(g(r, "id"))
            if csv:
                ref_tel[g(r, "id")] = csv
    except Garage61Error as e:
        if getattr(e, "fatal", False) or getattr(e, "status", None) == 401:
            raise
        say(f"  Comparison laps failed: {e}")   # 2.7.1 (M3): the review goes on with what it has
    refs = [r for r in refs if g(r, "id") in ref_tel]
    rc_found = _racecap_race(cfg, api, event_id, track_name, laps, my_name) if chosen_kind in (3, None) else None
    if rc_found:
        try:
            from . import racecap_link
            for lap, text in racecap_link.rival_laps(rc_found["dir"], rc_found["car"].get("number")):
                refs.append(lap)
                ref_tel[lap["id"]] = text
        except Exception:  # noqa: BLE001  (RaceCap is optional: its laps are never worth losing the review over)
            import traceback
            say("  Couldn't read RaceCap's rival laps:\n" + traceback.format_exc())
    step(f"  {len(refs)} comparison lap(s) with telemetry." if refs else
         "  No comparison laps (follow a few drivers in this car on Garage 61 to get some).", 0.8)
    step("Checking which teammates and followed drivers were on track...", 0.82)
    classes, sched_mc = None, None
    starts = [lap_start_dt(l) for l in laps if lap_start_dt(l)]
    try:
        from . import schedule
        sched = schedule.load_current(min(starts).date() if starts else None)
        classes = schedule.class_lookup(sched)
        if starts:
            sched_mc = schedule.multiclass_at(sched, track_name, car_name, min(starts).astimezone(timezone.utc).date())
    except Exception:  # noqa: BLE001  (no schedule loaded: classes stay unknown)
        classes = None
    others = on_track(api, _id(track), event_id, slug, car, classes)
    # multi-class if the schedule says so, or if a teammate/followed driver was there in another class
    multiclass = bool(sched_mc) or bool(others.get("multiclass"))
    others["multiclass"] = multiclass
    if multiclass:
        step("  Multi-class race: laps that lost time to traffic will be left out of your pace numbers.")
    meta = {"driver": my_name, "event_id": event_id, "car": car_name, "track": track_name,
            "started_at": min(starts).isoformat() if starts else "",
            # 2.7.1 (H1): from the laps actually reviewed, never "Session" (which the library used to read as
            # not-a-race, so "Review my last race" with practice included vanished from PBs and trends)
            "session": session_label(chosen_kind),
            # how the review addresses you (settings: salutation); empty = neutral
            "salutation": normalise_salutation(cfg.get("salutation", "")),
            "multiclass": multiclass,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for l in laps:  # normalise fields the engine relies on
        l["clean"] = is_clean(l)
        if lap_start_dt(l):
            l["_start"] = lap_start_dt(l).timestamp()
    step("Analysing...", 0.84)
    try:
        data = engine.analyze(meta, laps, tel, refs, ref_tel, track_length=track_length(api, track), log=say)
    except ValueError as e:
        raise SystemExit(f"Couldn't build the review: {e}")
    except IndexError:
        # 2.7 (M5): a zero/garbage Speed channel makes the lap's distance grid empty and the engine's resample
        # fails on an empty array; never Python's own words to the user
        import traceback
        say("  The engine couldn't place a lap on the track (broken speed channel?):\n" + traceback.format_exc())
        raise SystemExit(BROKEN_SPEED_MSG)
    data["on_track"] = others
    if rc_found:
        try:
            from . import racecap_link
            racecap_link.attach(data, rc_found)
        except Exception:  # noqa: BLE001
            import traceback
            say("  Couldn't add RaceCap's race log:\n" + traceback.format_exc())
    data["meta"]["requests"] = api.requests_made
    step("Saving the review and updating the library...", 0.95)
    # a practice or qualifying review of the same event gets its own folder, next to the race's
    if chosen_kind in (3, None) and hasattr(library, "retire_legacy_mixed"):
        starts = [dt.timestamp() for dt in (lap_start_dt(l) for l in laps) if dt]
        library.retire_legacy_mixed(library.review_path(cfg["library_folder"], laps, track_name, car_name),
                                    race_start=min(starts) if starts else None, log=say)
    out_dir = library.review_dir(cfg["library_folder"], laps, track_name, car_name,
                                 suffix={1: "practice", 2: "qualifying"}.get(
                                     chosen_kind, ""))
    # personal-best check against earlier reviews of this track and car (no Garage 61 requests)
    try:
        data["pb"] = library.pb_for(cfg["library_folder"], data, exclude_dir=out_dir.name)
    except Exception:  # noqa: BLE001  (a PB banner is never worth losing the review over)
        import traceback
        say("  Couldn't check for a personal best:\n" + traceback.format_exc())
    # re-reviewing a race keeps the result you typed in earlier (result.json in the review folder)
    try:
        from . import results
        saved = results.load(out_dir)
        if saved and saved.get("finish_pos") is not None:
            data["result"] = saved
    except Exception:  # noqa: BLE001
        import traceback
        say("  Couldn't read the saved race result:\n" + traceback.format_exc())
    learn = learn_from_for(cfg["library_folder"], data)
    if learn:
        data["learn_from"] = learn
    if rebuild:
        try:
            old = json.loads((out_dir / "review.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        info["changes"] = rebuild_changes(old, data)
    page = report.write(out_dir, data)
    try:
        library.rebuild(cfg["library_folder"])
    except Exception:  # noqa: BLE001  (the review is saved; the library page is rebuilt when next opened)
        import traceback
        say("  Couldn't update the library page (it will refresh next time it's opened):\n" + traceback.format_exc())
    step(f"Done. Review saved to {page}", 1.0)
    say(f"Garage 61 requests this run: {api.requests_made}")
    if open_after:
        webbrowser.open(page.as_uri())
    return page


def learn_from_for(library_folder, data) -> list:
    """2.5 "who to learn from" for a new review: team.learn_from_saved reads <library>/team/team.json and
    matches its track and car (no requests). [] when there's no team data for this combo, when the team
    module is older, or when anything goes wrong (never worth losing the review over)."""
    try:
        from . import team
        fn = getattr(team, "learn_from_saved", None)
        out = fn(library_folder, data) if callable(fn) else None
    except Exception:  # noqa: BLE001
        import traceback
        say("  Couldn't work out who to learn from:\n" + traceback.format_exc())
        return []
    return [x for x in out if isinstance(x, dict)] if isinstance(out, list) else []


def optional_module(name: str):
    """Import a feature module (prep, goals, results, team) without letting a broken one stop the app.
    Returns the module, or None after writing the reason to the log."""
    import importlib
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except Exception:  # noqa: BLE001
        import traceback
        say(f"ERROR loading the {name} module:\n" + traceback.format_exc())
        return None


def item_name(it) -> str:
    """'Name (Variant)': the same display name name_of() gives cars and tracks in reviews."""
    nm = str(g(it, "name", default="") or "")
    v = g(it, "variant")
    return nm + (f" ({v})" if v else "")


def id_for_name(items, name) -> int | None:
    """Garage 61 id of a car/track from its display name, using a list you already have."""
    want = str(name or "").strip().lower()
    for it in items or []:
        if item_name(it).strip().lower() == want:
            return g(it, "id")
    return None


def _job_logger(progress, start=0.08, end=0.92):
    """log() for modules that only print messages: each message nudges the bar a little further."""
    step = _stepper(progress)
    n = [0]

    def log(msg):
        n[0] += 1
        step(msg, start + (end - start) * (1 - 0.82 ** n[0]))
    return step, log


def prep_strategy(cfg, api, data) -> dict:
    """{"strategy": prefill} for the prep page's "Fuel for this race" box (2.4), or {} when there's nothing to
    show. No requests: strategy.prefill reads the library, the sync store, the schedule and strategy.json."""
    strategy = optional_module("strategy")
    if strategy is None or not data.get("track") or not data.get("car"):
        return {}
    try:
        from . import schedule, sync
        sched = schedule.load_current()
        week = None
        if sched:
            from datetime import datetime, timezone
            wk = schedule.week_on(sched, datetime.now(timezone.utc).date())
            for ser in schedule.series_at(sched, data["track"], data["car"], datetime.now(timezone.utc).date()) if wk else []:
                week = next((w for w in ser.get("weeks") or [] if w.get("season_week", w.get("week")) == wk), None)
                if week:
                    break
        store = sync.Store.for_api(api) if api is not None else None
        pre = strategy.prefill(cfg, store, sched, data["track"], [data["car"]], week)
    except Exception:  # noqa: BLE001  (a prep page never fails over the fuel box)
        import traceback
        say("ERROR working out the fuel box for the prep page:\n" + traceback.format_exc())
        return {}
    return {"strategy": pre} if isinstance(pre, dict) else {}


def cmd_prep(cfg, track_id, car_id, progress=None, open_after=False) -> Path:
    """Build the track prep page for one track/car. Requests: whatever prep.build reports (bounded there)."""
    prep = optional_module("prep")
    if prep is None:
        raise SystemExit("Track prep isn't available in this copy of Debrief (details are in debrief.log).")
    try:
        track_id, car_id = int(track_id), int(car_id)
    except (TypeError, ValueError):
        raise SystemExit("Pick a track and a car first.")
    step, log = _job_logger(progress)
    step("Getting your history at this track and car...", 0.04)
    api = make_api(cfg)
    data = prep.build(api, cfg["library_folder"], track_id, car_id, cfg.get("team_slug") or None, log=log,
                      members=cfg.get("team_members"))
    data.update(prep_strategy(cfg, api, data))
    step("Saving the prep page...", 0.95)
    page = prep.write(cfg["library_folder"], data)
    step(f"Done. Prep page saved to {page}", 1.0)
    say(f"Garage 61 requests this run: {api.requests_made}")
    if open_after:
        webbrowser.open(Path(page).as_uri())
    return Path(page)


def cmd_team(cfg, progress=None, open_after=False) -> Path:
    """Refresh the team hub. Worst case is stated in team.py (58 requests for 6 track/car combos, cold cache)."""
    from . import team
    step, log = _job_logger(progress)
    step("Connecting to Garage 61...", 0.03)
    api = make_api(cfg)
    data = team.build(api, cfg["library_folder"], cfg.get("team_slug") or DEFAULT_TEAM, log=log, max_combos=6, cfg=cfg)
    step("Saving the team hub...", 0.96)
    data["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["cache_reused"] = (data.get("requests") or 0) == 0
    page = team.write(cfg["library_folder"], data)
    if data["cache_reused"]:   # 2.21: say so, instead of looking like nothing happened
        step("Up to date, checked " + datetime.now().strftime("%I:%M %p").lstrip("0").lower()
             + ". Garage 61 had nothing newer than the last refresh.", 1.0)
    else:
        step(f"Done. Team hub saved to {page}", 1.0)
    say(f"Garage 61 requests this run: {api.requests_made}")
    if open_after:
        webbrowser.open(Path(page).as_uri())
    return Path(page)


def cmd_team_load(cfg, track, car, progress=None) -> Path:
    """2.21: load one track and car on the Team page ("Load this track"); the saved page is updated."""
    from . import team
    step, log = _job_logger(progress)
    step("Connecting to Garage 61...", 0.03)
    api = make_api(cfg)
    data = team.load_combo(api, cfg["library_folder"], track, car, cfg.get("team_slug") or DEFAULT_TEAM, log=log, cfg=cfg)
    step("Saving the team hub...", 0.96)
    data["cache_reused"] = (data.get("requests") or 0) == 0
    page = team.write(cfg["library_folder"], data)
    step(f"Done. Loaded {library_short(track)}. Garage 61 requests: {api.requests_made}", 1.0)
    return Path(page)


def library_short(track) -> str:
    from . import library
    return library.short_track_name(track)


def cmd_weekly(cfg, progress=None, series_ids=None, force_old=False, open_after=False) -> dict:
    """This week's team leaderboards for your series (2.5), then the team page rebuilt from team.json and the
    weekly snapshots. Requests (leaderboards.py): 1 per series, 2 if a "best" lap needs a second look, at
    most 8 series; 0 when a snapshot is under 10 minutes old or the week is final."""
    lb = optional_module("leaderboards")
    if lb is None:
        raise SystemExit("Weekly leaderboards aren't available in this copy of Debrief (details are in debrief.log).")
    from . import schedule, series, team
    sched = schedule.load_current()
    if not sched:
        raise SystemExit("Load the season schedule first (Prepare > Season schedule), then pick your series.")
    lib = cfg["library_folder"]
    mine = series.effective(lib, sched)    # 2.21: your saved series, else the one picked on Prepare
    step, log = _job_logger(progress)
    step("Connecting to Garage 61...", 0.03)
    api = make_api(cfg)
    summary = lb.refresh(api, cfg, sched, mine, series_ids=series_ids, force_old=bool(force_old), log=log)
    step("Saving the team page...", 0.96)
    page = team.render_saved(lib, cfg)
    step(f"Done. Team page saved to {page}", 1.0)
    say(f"Garage 61 requests this run: {api.requests_made}")
    if open_after:
        webbrowser.open(Path(page).as_uri())
    return {"page": Path(page), "requests": api.requests_made, "summary": summary if isinstance(summary, dict) else {}}


def cmd_sync(cfg, progress=None, should_stop=None, open_after=False) -> dict:
    """Bring the local copy of your Garage 61 history up to date (see sync.py). The first run
    downloads everything; after that only new laps. Stops cleanly when should_stop() is True."""
    from . import sync
    step = _stepper(progress)
    step("Connecting to Garage 61...", 0.01)
    api = make_api(cfg)
    return sync.run(api, log=say, progress=progress, should_stop=should_stop, telemetry=cfg.get("sync_telemetry", "best"))


def cmd_check(cfg, progress=None, open_after=False):
    """Test the token. Returns the result lines (a list of strings, as before; see CheckLines for the
    neutral notes). open_after is accepted for symmetry (nothing to open)."""
    lines = CheckLines()
    info = lines.info
    step0 = _stepper(progress)

    def step(msg, frac=None):
        lines.append(str(msg))
        step0(msg, frac)
    if progress:
        progress(0.05, "Checking the token with Garage 61...")
    api = make_api(cfg)
    # always live, so a bad token is caught (1 request, same as before); the answer is also saved in the
    # cache like api.me() does, so the window can greet you by name without asking Garage 61 again
    me = api._cached("me", lambda: api._get("me"), max_age_s=0)
    if not me:
        raise SystemExit("Garage 61 returned nothing for your account. Check the token in Settings.")
    step(f"Token works. Account: {g(me, 'slug')}", 0.2)
    step(f"Teams: {', '.join(str(g(t, 'name', 'slug', default='?')) for t in api.teams()) or 'none'}", 0.35)
    combos = recent_combos(api, 30, cfg["cache_dir"])
    step(f"Track/car combinations driven in the last 30 days: {len(combos)}", 0.55)
    recent = laps_for(api, combos[0][0], combos[0][1], age=30, limit=5) if combos else []
    step(f"Recent laps visible: {len(recent)}", 0.7)
    if recent:
        csv = api.lap_csv(g(recent[0], "id"))
        if csv:
            step("Telemetry download: works", 0.9)
        else:
            step(TELEMETRY_INFO, 0.9)
            info.append(TELEMETRY_INFO)
        dbg = Path(cfg["cache_dir"]) / "sample_lap.json"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        dbg.write_text(json.dumps(recent[0], indent=2), encoding="utf-8")
        step(f"Saved one sample lap record to {dbg} (handy if something needs fixing; it has no token in it).")
    step(f"Library folder: {cfg['library_folder']}", 0.97)
    step(f"Requests used: {api.requests_made}", 1.0)
    return lines


TELEMETRY_INFO = ("Telemetry: not included with your Garage 61 plan. Race reviews need lap telemetry, so check "
                  "your plan on garage61.net. Sessions, history and team pages still work.")


class CheckLines(list):
    """cmd_check's result: still a list of lines (old callers keep working), plus .info, the lines that are
    neutral notes rather than problems, and .rows(): [{"text", "level"}] with level "info" for those and
    "ok" for the rest. Nothing in it is an error."""

    def __init__(self, *a):
        super().__init__(*a)
        self.info = []

    def rows(self) -> list:
        return [{"text": t, "level": "info" if t in self.info else "ok"} for t in self]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="debrief")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("review"); r.add_argument("--event"); r.add_argument("--no-open", action="store_true")
    sub.add_parser("check"); sub.add_parser("library")
    pp = sub.add_parser("prep", help="build a track prep page")
    pp.add_argument("--track", type=int, required=True, help="Garage 61 track id")
    pp.add_argument("--car", type=int, required=True, help="Garage 61 car id")
    pp.add_argument("--no-open", action="store_true")
    sub.add_parser("overlay-test", help="open the coach overlay for 15 seconds with sample text, no iRacing needed")
    sub.add_parser("sync", help="download your Garage 61 history (first time: everything; then new laps)")
    tp = sub.add_parser("team", help="update the team hub")
    tp.add_argument("--no-open", action="store_true")
    wp = sub.add_parser("weekly", help="update this week's team leaderboards for your series")
    wp.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_settings()
    global LOG_FILE
    LOG_FILE = str(HERE / "debrief.log")
    say(f"--- {datetime.now().isoformat(timespec='seconds')} debrief {a.cmd or 'review'} (Python {sys.version.split()[0]})")
    try:
        if a.cmd == "check":
            cmd_check(cfg)
        elif a.cmd == "library":
            p = library.rebuild(cfg["library_folder"]); say(f"Library: {p}"); webbrowser.open(p.as_uri())
        elif a.cmd == "prep":
            cmd_prep(cfg, a.track, a.car, open_after=not a.no_open)
        elif a.cmd == "team":
            cmd_team(cfg, open_after=not a.no_open)
        elif a.cmd == "weekly":
            cmd_weekly(cfg, open_after=not a.no_open)
        elif a.cmd == "sync":
            cmd_sync(cfg)
        elif a.cmd == "overlay-test":
            from . import overlay
            code = overlay.run_test(out=say)
            if code:
                raise SystemExit("The overlay did not open. The reason is above and in the Debrief log.")
        else:
            cmd_review(cfg, getattr(a, "event", None), open_after=not getattr(a, "no_open", False))
    except Garage61Error as e:
        say(f"ERROR Garage 61: {e}")
        raise SystemExit(f"Garage 61: {e}")
    except SystemExit as e:
        if e.code not in (None, 0):
            say(f"STOPPED: {e.code}")
        raise
    except Exception:
        import traceback
        say("ERROR (unexpected):\n" + traceback.format_exc())
        raise SystemExit("Something unexpected went wrong. Details were saved to the Debrief log.")


if __name__ == "__main__":
    main()
