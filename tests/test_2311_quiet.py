"""Debrief 2.31.1: a window that stops waiting is not an error, and a background error banner hides after its time.

    python3 tests/ci_run.py tests/test_2311_quiet.py

1. The server: when the window has gone away before a reply is written (a click to another page, a reload),
   Windows raises ConnectionAbortedError and Linux BrokenPipeError. Debrief used to log that as "ERROR in api/..." with
   a traceback, then try to write a 500 to the same closed connection. Now the reply is dropped quietly.
2. The window: banner(msg, true, ms) with a time hides after that time (sync, schedule and telemetry errors pass 9 s);
   banner(msg, true) with no time, and any time 0, still stays until closed.
No network, temp folders from tempfile, utf-8 everywhere.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-2311-"))
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

from debrief import app  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

FAILS = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


class _Gone(io.RawIOBase):
    def __init__(self, exc):
        self.exc = exc

    def write(self, b):
        raise self.exc

    def writable(self):
        return True


def _handler(exc):
    h = S.Handler.__new__(S.Handler)   # no socket: only what _send and _carsetup_call use
    h.wfile = _Gone(exc)
    h.request_version = "HTTP/1.1"
    h.command = "GET"
    h.close_connection = False
    h._headers_buffer = []
    h.headers = {"X-Debrief": "1"}
    h.requestline = "GET /x HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.server = None
    h.log_message = lambda *a, **k: None
    return h


def test_server_quiet():
    said = []
    real = app.say
    app.say = lambda m, *a, **k: said.append(str(m))
    try:
        for exc in (BrokenPipeError(32, "Broken pipe"), ConnectionAbortedError(10053, "aborted"),
                    ConnectionResetError(10054, "reset")):
            said.clear()
            h = _handler(exc)
            raised = None
            try:
                h._carsetup_call(lambda: h._send(200, b'{"ok": true}', "application/json"), "api/handling", get=True)
            except Exception as e:  # noqa: BLE001
                raised = e
            name = type(exc).__name__
            check(raised is None, f"{name}: nothing is raised out of the request")
            check(not any("ERROR" in m for m in said), f"{name}: nothing is logged as an ERROR")
            check(h.close_connection, f"{name}: the connection is closed")
        # a real failure inside a route is still logged and answered
        said.clear()
        h = _handler(None)
        h.wfile = io.BytesIO()

        def boom():
            raise RuntimeError("a real problem")
        h._carsetup_call(boom, "api/handling", get=True)
        check(any("ERROR in api/handling" in m for m in said), "a real failure is still logged as an ERROR")
        check(b"500" in h.wfile.getvalue().split(b"\r\n", 1)[0], "a real failure still answers 500")
    finally:
        app.say = real


def test_banner_times():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    start = js.index("function banner(")
    end = js.index("\n$('bannerClose')", start)
    fn = js[start:end]
    code = """
const timers=[];global.setTimeout=(f,ms)=>{timers.push(ms);return timers.length};global.clearTimeout=()=>{};
const el={hidden:true,className:''};const txt={textContent:''};
global.$=id=>id==='banner'?el:id==='bannerText'?txt:null;global.plainText=s=>s;
""" + fn + """
const out={};
for(const [k,args] of Object.entries({bad_with_time:['x',true,9000],bad_no_time:['x',true],bad_zero:['x',true,0],
  ok_default:['x'],ok_time:['x',false,12000],ok_zero:['x',false,0]})){timers.length=0;banner(...args);out[k]=timers.slice()}
process.stdout.write(JSON.stringify(out));"""
    try:
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        print("  SKIPPED: node not available")
        return
    check(r.returncode == 0, "banner() runs in node: " + (r.stderr.strip()[:200] or "ok"))
    if r.returncode:
        return
    o = json.loads(r.stdout)
    check(o["bad_with_time"] == [9000], "an error banner with a time hides after it (the sync error after 9 s)")
    check(o["bad_no_time"] == [], "an error banner with no time stays until closed")
    check(o["bad_zero"] == [], "an error banner with time 0 stays (Debrief has stopped)")
    check(o["ok_default"] == [5000], "a plain banner still hides after 5 s")
    check(o["ok_time"] == [12000], "a plain banner with a time hides after it")
    check(o["ok_zero"] == [], "a plain banner with time 0 stays")


if __name__ == "__main__":
    test_server_quiet()
    test_banner_times()
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
