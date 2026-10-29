"""Debrief desktop window: a local server plus Microsoft Edge in app mode.

    pythonw -m debrief.desktop              open the Debrief window
    python  -m debrief.desktop --no-browser print the URL instead (for testing)
"""
from __future__ import annotations

import argparse
import os
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
                   "analysis_plus", "schedule", "series", "notes", "prepare")


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
    threading.Thread(target=srv.watchdog, daemon=True, name="watchdog").start()
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
