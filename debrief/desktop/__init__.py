"""Debrief desktop window: a local server plus Microsoft Edge in app mode.

    pythonw -m debrief.desktop              open the Debrief window
    python  -m debrief.desktop --no-browser print the URL instead (for testing)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

from .. import app

# Feature modules the window loads by name (app.optional_module / lazy imports). The selftest imports
# each one, so a build that leaves one out fails in CI instead of on someone's PC.
FEATURE_MODULES = ("engine", "report", "library", "prep", "goals", "results", "team", "sync",
                   "analysis_plus", "schedule", "series", "notes", "prepare", "updater", "insights")


_REFRESH_DONE: set = set()
REFRESH_PAUSE = threading.Event()   # set while a job runs: the refresh steps aside between reviews


def _refresh_reviews():
    """Once per page-layout version: re-render saved reviews in the new layout (no Garage 61 requests).
    Gives way to your jobs (it pauses between reviews and the server restarts it when the job ends)."""
    try:
        from .. import report
        n = report.refresh_all(app.load_settings()["library_folder"], log=app.say,
                               should_stop=REFRESH_PAUSE.is_set, done=_REFRESH_DONE)
        if n:
            app.say(f"Refreshed {n} saved review(s) to the new page layout.")
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001
        app.say("ERROR refreshing saved reviews:\n" + traceback.format_exc())


_LOCK_PATH = re.compile(r"^/[A-Za-z0-9_-]{32}/$")


def valid_lock_url(url: str) -> bool:
    """Only an address this app itself would write: http://127.0.0.1:<port>/<32-character secret>/."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 200 or any(c.isspace() for c in url):
        return False
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "http" and u.hostname == "127.0.0.1" and port is not None and 0 < port < 65536
            and u.netloc == f"127.0.0.1:{port}" and not u.query and not u.fragment
            and bool(_LOCK_PATH.match(u.path)))


_MUTEX = None


def _instance_mutex() -> None:
    """Windows: a named mutex that says "Debrief is running". The installer waits for it to go before
    replacing files (packaging/debrief.iss), so an update never cuts Debrief off mid-write."""
    global _MUTEX
    if os.name != "nt" or _MUTEX is not None:
        return
    try:
        import ctypes
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "DebriefRunning")
    except Exception:  # noqa: BLE001
        _MUTEX = None


def _after_update(srv) -> None:
    """Did the last in-app update work? Also clears old installers out of %TEMP%. Never touches your data."""
    try:
        from .. import updater
        res = updater.pending_result()
        if res is not None:
            srv.update_outcome = res
            app.say("Update " + ("finished: now on " + res["version"] if res["ok"] else "didn't finish: " + res["message"]))
        n = updater.cleanup_old_downloads()
        if n:
            app.say(f"Removed {n} old update download(s) from the temp folder.")
    except Exception:  # noqa: BLE001
        app.say("ERROR checking the last update:\n" + traceback.format_exc())


def find_edge() -> str | None:
    cands = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    cands += [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
              Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
    for c in cands:
        if c.is_file():
            return str(c)
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                        v = winreg.QueryValue(k, None)
                        if v and Path(v).is_file():
                            return v
                except OSError:
                    continue
        except ImportError:
            pass
    return None


def open_window(url: str) -> str:
    edge = find_edge()
    if edge:
        profile = app.HERE / ".edge-profile"
        profile.mkdir(exist_ok=True)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen([edge, f"--app={url}", "--window-size=1400,900", f"--user-data-dir={profile}",
                          "--no-first-run", "--no-default-browser-check"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags)
        return "edge"
    app.say("Microsoft Edge wasn't found, so Debrief opened in your default browser. Its address works "
            "only while this Debrief is open.")
    webbrowser.open(url)
    return "browser"


def _alert(msg: str):
    """A message box when there's no console to print to (pythonw)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "Debrief", 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    print(msg, file=sys.stderr)


def selftest() -> int:
    """Start the server without a window, load the page, the status API and the UI assets, check the
    bundled templates, then write a one-line JSON result to <data home>/selftest.json.
    Exit code 0 = everything loaded, 1 = something didn't. Never contacts Garage 61."""
    import json
    import time
    import urllib.request
    from .. import __version__
    # a selftest must never hang a build: after 90 s, write every thread's stack to selftest-hang.txt and exit
    import faulthandler
    try:
        _hang = open(app.HERE / "selftest-hang.txt", "a", encoding="utf-8")
        _hang.write("selftest() reached\n")
        _hang.flush()
        faulthandler.dump_traceback_later(90, exit=True, file=_hang)
    except Exception:  # noqa: BLE001
        _hang = None
    checks, error = {}, None
    srv = None
    try:
        from .server import DebriefServer, FALLBACK_TOKENS, design_tokens
        from .. import report
        import numpy  # noqa: F401  (bundled dependencies import)
        import requests  # noqa: F401
        import pypdf  # noqa: F401  (season schedule PDF reader)
        from .. import sync  # noqa: F401  (history sync)
        checks["imports"] = True
        # every page's code must load, including the ones only imported by name (2.1.0 lost prep this way)
        import importlib
        for mod in FEATURE_MODULES:
            try:
                importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{mod}")
                checks["module:" + mod] = True
            except Exception:  # noqa: BLE001
                app.say(f"Selftest: module {mod} failed to load:\n" + traceback.format_exc())
                checks["module:" + mod] = False
        tpl = Path(report.TPL)
        for name in ("review.html", "library.html", "prep.html", "team.html"):
            p = tpl / name
            checks["template:" + name] = p.is_file() and p.stat().st_size > 1000
        checks["settings.example.toml"] = (app.RES / "settings.example.toml").is_file()
        checks["design_tokens"] = design_tokens() != FALLBACK_TOKENS
        srv = DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 only, never a proxy

        def get(rel, want=None):
            rq = urllib.request.Request(srv.url + rel, headers={"X-Debrief": "1"})
            try:
                with opener.open(rq, timeout=15) as r:
                    body = r.read()
                    return r.status == 200 and len(body) > 0 and (want is None or want in body)
            except Exception:  # noqa: BLE001
                return False
        checks["page"] = get("", b'id="vWelcome"')
        checks["api/status"] = get("api/status", b'"state"')
        checks["api/settings"] = get("api/settings", b'"version"')
        # the Prepare page's data builds (schedule, series and track cards) without settings or a schedule
        import tempfile
        from .. import prepare
        with tempfile.TemporaryDirectory() as td:
            checks["prepare"] = prepare.view({"library_folder": td}, None, []).get("ok") is True
        for asset, want in (("static/app.js", b"use strict"), ("static/app.css", b"box-sizing"),
                            ("static/tokens.css", b":root"), ("static/icon.png", b"PNG")):
            checks[asset] = get(asset, want)

        def head(rel, name):
            try:
                with opener.open(urllib.request.Request(srv.url + rel), timeout=15) as r:
                    return r.headers.get(name) or ""
            except Exception:  # noqa: BLE001
                return ""
        # app.js must be served as JavaScript whatever the PC's registry says (or the window opens blank)
        checks["type:app.js"] = head("static/app.js", "Content-Type").startswith("text/javascript")
        checks["type:app.css"] = head("static/app.css", "Content-Type").startswith("text/css")
        checks["csp:page"] = "default-src 'self'" in head("", "Content-Security-Policy")
        # library pages run sandboxed (they can't reach the API)
        from .server import LIB_CSP
        checks["csp:lib"] = "sandbox" in LIB_CSP and "allow-same-origin" not in LIB_CSP
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        app.say("ERROR in selftest:\n" + traceback.format_exc())
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass
    ok = error is None and bool(checks) and all(checks.values())
    result = {"ok": ok, "version": __version__, "frozen": app.FROZEN, "time": int(time.time()),
              "failed": sorted(k for k, v in checks.items() if not v), "error": error}
    line = json.dumps(result, separators=(",", ":"))
    try:
        (app.HERE / "selftest.json").write_text(line + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError):
        pass
    app.say(f"Selftest {'passed' if ok else 'FAILED'}: {line}")
    try:
        faulthandler.cancel_dump_traceback_later()
        if _hang:
            _hang.write("selftest finished\n")
            _hang.close()
    except Exception:  # noqa: BLE001
        pass
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="debrief.desktop")
    ap.add_argument("--no-browser", action="store_true", help="don't open a window; print the URL")
    ap.add_argument("--selftest", action="store_true",
                    help="start without a window, check the app loads, write selftest.json and exit")
    ap.add_argument("--heartbeat-timeout", type=float, default=90.0, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if sys.stdout is None:  # pythonw: nowhere to print
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    app.LOG_FILE = str(app.HERE / "debrief.log")
    app.ensure_settings()  # installed app only: first start creates settings.toml for first-run setup
    if a.selftest:
        return selftest()
    lock = app.HERE / ".debrief-running"
    if not a.no_browser and lock.exists():
        try:  # already open? bring up the same window instead of starting a second server
            import urllib.request
            url = lock.read_text(encoding="utf-8").strip()
            if not valid_lock_url(url):
                raise ValueError("not a Debrief address")   # treated like a stale lock: start normally
            # the server only answers POST + X-Debrief on api/ping; never go through a proxy for localhost
            req = urllib.request.Request(url + "api/ping", data=b"", method="POST", headers={"X-Debrief": "1"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=2) as r:
                if r.status == 200:
                    open_window(url)
                    return 0
        except Exception:  # noqa: BLE001  (stale lock from a crash: carry on)
            pass
    app.say(f"--- {datetime.now().isoformat(timespec='seconds')} debrief desktop start (Python {sys.version.split()[0]})")
    try:
        from .server import DebriefServer
        srv = DebriefServer(heartbeat_timeout=a.heartbeat_timeout)
    except Exception:  # noqa: BLE001
        app.say("ERROR starting desktop server:\n" + traceback.format_exc())
        _alert(f"Debrief couldn't start. Details were saved to debrief.log in {app.data_folder_label()}.")
        return 1
    _instance_mutex()
    _after_update(srv)
    threading.Thread(target=srv.watchdog, daemon=True, name="watchdog").start()
    # tracked, so the watchdog, an update and new jobs wait for it (it rewrites saved reviews)
    srv.jobs.refresh_fn = _refresh_reviews
    srv.jobs.refresh_pause = REFRESH_PAUSE
    srv.jobs.run_background("refresh", _refresh_reviews)
    if not a.no_browser:
        try:
            lock.write_text(srv.url, encoding="utf-8")
        except OSError:
            pass
    if a.no_browser:
        print(srv.url, flush=True)
    else:
        try:
            how = open_window(srv.url)
            app.say(f"Window opened ({how}).")
        except Exception:  # noqa: BLE001
            app.say("ERROR opening the window:\n" + traceback.format_exc())
            _alert(f"Debrief couldn't open its window. Details were saved to debrief.log in {app.data_folder_label()}.")
            return 1
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            if not a.no_browser and lock.exists() and lock.read_text(encoding="utf-8").strip() == srv.url:
                lock.unlink()
        except OSError:
            pass
    return 0
