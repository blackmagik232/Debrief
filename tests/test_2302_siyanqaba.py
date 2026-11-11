"""2.30.2 Siyanqaba: "How did it feel" taps (feel.py), the night shift folder (zerverpush.py), Siyanqaba's summary
(aisummary.py, with a stand-in for the Zerver), Settings > Siyanqaba, and the page's endpoints through the real server
(port 0, the X-Debrief header) on fake .ibt files (tests/corner_laps.py). No network: the Zerver is a function here.

    python3 tests/test_232_siyanqaba.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-232-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import aisummary, app, c1, factsheet, feel, zerverpush as zp  # noqa: E402
from debrief.desktop import server as S, settings_io  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    return None


SID = "0123456789abcdef"

print("== feel.py: taps on disk")
store = feel.FeelStore(TMP / "lib" / "feel")
d = store.add(SID, {"track_key": "zolder gp", "car_path": "mclaren570sgt4", "length_m": 3980}, "C3", 0.378, "loose", "exit")
check(d["taps"][-1] == {**d["taps"][-1], "corner": "C3", "apex_pct": 0.378, "tag": "loose", "phase": "exit"}, "a tap is saved")
check(json.loads(store.path(SID).read_text(encoding="utf-8"))["feel_version"] == 1, "the file says feel_version 1")
store.add(SID, {}, "C6", 0.79, "lockup")
check([t["tag"] for t in store.load(SID)["taps"]] == ["loose", "lockup"] and store.load(SID)["track_key"] == "zolder gp",
      "taps add up and keep the session's track")
check(feel.summary(store.load(SID)) == ["Turn 6: Lockup", "Turn 3: Loose (exit)"], "summary, newest first")
check(store.undo(SID)["taps"][-1]["tag"] == "loose", "undo takes the last tap back")
check(raises(store.add, SID, {}, "C3", 0.4, "wobbly") is not None, "an unknown tag is refused")
check(raises(store.add, SID, {}, "C3", 0.4, "snap", "apex") is not None, "an unknown phase is refused")
check(raises(store.add, SID, {}, "../x", 0.4, "snap") is not None, "a strange corner is refused")
check(raises(store.path, "../../etc") is not None, "a strange session id never becomes a path")
check(store.load("fedcba9876543210")["taps"] == [], "a session with no file has no taps")

print("== zerverpush.py: the night shift folder")
src = TMP / "tel"
src.mkdir()
f1 = src / "a.ibt"
f1.write_bytes(b"x" * 1000)
old = time.time() - 600
os.utime(f1, (old, old))
dest = TMP / "zerver"
sent = TMP / "cache" / "zerver-sent.json"
r = zp.push(str(dest), SID, [f1], sent_file=sent)
check(r["error"] and "can't reach" in r["error"], "a missing folder (Zerver off) says so")
dest.mkdir()
r = zp.push(str(dest), SID, [f1], feel_file=store.path(SID), sent_file=sent, info={"car_path": "mclaren570sgt4"})
check(not r["error"] and r["sent"] == ["a.ibt"], "the file is sent")
check((dest / "a.ibt").read_bytes() == f1.read_bytes() and not list(dest.glob("*.partial")), "copied whole, no .partial left")
man = json.loads((dest / f"{SID}.session.json").read_text(encoding="utf-8"))
check(man["files"] == ["a.ibt"] and man["session_id"] == SID, "the manifest names the session's files")
check((dest / "feel" / f"{SID}.json").is_file(), "the feel file goes with it")
check(f1.read_bytes() == b"x" * 1000 and f1.exists(), "the telemetry file is untouched (only read)")
check(zp.is_sent(sent, [f1]), "remembered as sent")
check(zp.push(str(dest), SID, [f1], sent_file=sent)["skipped"] == ["a.ibt"], "a second send skips it")
fresh = src / "b.ibt"
fresh.write_bytes(b"y" * 10)
r = zp.push(str(dest), "1123456789abcdef", [fresh], sent_file=sent)
check(r["error"] and "still be writing" in r["error"] and not (dest / "b.ibt").exists(), "a file still being written waits")
check([s["id"] for s in zp.pending([{"id": "old", "paths": [f1], "sort": time.time()},
                                    {"id": "new", "paths": [fresh], "sort": time.time()},
                                    {"id": "ancient", "paths": [fresh], "sort": time.time() - 30 * 86400}], sent)] == ["new"],
      "pending: last week's unsent sessions only")

print("== aisummary.py: order only, checked, plain on any trouble")
cl_ = {"focus": "in Turn 6 your best laps were about 0.25 seconds quicker", "best": "best lap 1:35.038",
       "fuel": "fuel use 2.45 liters a lap", "consistency": "4 of 13 clean laps within half a second of your best"}
seen = {}


def zerver(order):
    def post(url, body, timeout):
        seen.update(url=url, body=body, timeout=timeout)
        if isinstance(order, Exception):
            raise order
        return {"message": {"content": json.dumps({"order": order})}}
    return post


got = aisummary.ordered("10.0.0.20", cl_, ["focus"], post=zerver(["fuel", "best", "consistency"]))
check(got["source"] == "siyanqaba" and got["text"].startswith("In Turn 6") and got["text"].index("Fuel use") < got["text"].index("Best lap"),
      "the model's order, required line first")
check(seen["url"] == "http://10.0.0.20:11434/api/chat" and seen["body"]["model"] == aisummary.MODEL, "asks the pinned model on the Zerver")
check("focus" not in seen["body"]["messages"][1]["content"], "the required line isn't even offered to the model")
check(seen["timeout"] == aisummary.TIMEOUT_S, "with the timeout")
for bad, why in ((["fuel", "best"], "a line missing"), (["fuel", "best", "best", "consistency"], "a line twice"),
                 (["fuel", "best", "weather"], "an invented line"), (urllib.error.URLError("down"), "the Zerver off"),
                 (TimeoutError("slow"), "a timeout")):
    g = aisummary.ordered("10.0.0.20", cl_, ["focus"], post=zerver(bad))
    check(g["source"] == "plain" and g["text"] == aisummary.plain(cl_, ["focus"]) and g["problems"], f"plain order on {why}")
check(aisummary.ordered("", cl_, ["focus"])["source"] == "plain", "no address: plain, nothing sent")
for bad in ("8.8.8.8", "example.com", "http://10.0.0.5/api", "ftp://10.0.0.5"):
    check(raises(aisummary.clean_address, bad) is not None, f"refused: {bad}")
check(aisummary.clean_address("zerver") == "http://zerver:11434", "a bare name gets Ollama's port")

print("== Settings > Siyanqaba")
check(raises(settings_io.clean_night_shift_folder, str(TMP / "nope")) is not None, "a folder that isn't there is refused")
check(settings_io.clean_night_shift_folder(str(dest)) == str(dest), "an existing folder is kept")
check(settings_io.clean_night_shift_folder("") == "", "empty = off")
check(settings_io.clean_siyanqaba_address("10.0.0.20") == "http://10.0.0.20:11434", "the address is cleaned")

print("== through the window's server")
LIB, IR = TMP / "lib", TMP / "iRacing"
TEL = IR / "telemetry"
TEL.mkdir(parents=True)
Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
    'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
    f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IR}'\n", encoding="utf-8")
ibt = cl.make(TEL / "mclaren570sgt4_zolder gp 2026-11-03 18-00-00.ibt", 15, seed=21, session="Practice")
os.utime(ibt, (old, old))
app.LOG_FILE = str(TMP / "debrief.log")
srv = S.DebriefServer(heartbeat_timeout=3600)
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(rel, body=None):
    h = {"X-Debrief": "1"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(srv.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with op.open(rq, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


code, _r = call("api/setuplab/read", {})
end = time.time() + 180
while time.time() < end:
    _c, st = call("api/status")
    if st.get("state") != "running":
        break
    time.sleep(0.2)
_c, ss = call("api/sessions-ibt")
sess = (ss.get("sessions") or [{}])[0]
check(bool(sess.get("id")), "the session is listed")
code, j = call("api/siyanqaba/session?id=" + urllib.parse.quote(sess["id"]))
check(code == 200 and j["ok"] and j["corners"] and j["summary"]["text"], f"session: corners and a plain summary ({code})")
check(j["session_id"] == c1.session_id([ibt]), "the page uses the contract's session id")
check(j["zerver"]["set"] is False and j["ai_set"] is False, "Siyanqaba off by default")
corner = j["corners"][1]
code, t = call("api/feel", {"id": sess["id"], "corner": corner["id"], "tag": "snap", "phase": "entry"})
check(code == 200 and t["feel"]["lines"] == [f"Turn {corner['id'][1:]}: Snap (entry)"], "a tap from the page")
saved = feel.FeelStore(feel.folder_for(LIB)).load(j["session_id"])
check(saved["taps"][0]["apex_pct"] == corner["apex_pct"] and saved["car_path"] == "mclaren570sgt4",
      "saved in the library with where the corner is")
code, t = call("api/feel", {"id": sess["id"], "corner": "C99", "tag": "snap"})
check(code == 400 and not t["ok"], "a corner this session doesn't have is refused")
code, _t = call("api/zerver/push", {"id": sess["id"]})
check(code == 400, "Send now without a folder says what to do")
code, v = call("api/settings", {"night_shift_folder": str(dest), "siyanqaba_address": "127.0.0.1:9"})
check(code == 200 and v["ok"] and v["settings"]["night_shift_folder_ok"], "Settings saves the folder and address")
code, p = call("api/zerver/push", {"id": sess["id"]})
check(code == 200 and p["zerver"]["sent"] and (dest / ibt.name).is_file(), "Send to the Zerver now")
check((dest / "feel" / f"{j['session_id']}.json").is_file(), "the taps went with it")
call("api/feel/undo", {"id": sess["id"]})
check(json.loads((dest / "feel" / f"{j['session_id']}.json").read_text(encoding="utf-8"))["taps"] == [],
      "a later change reaches the Zerver's copy too")
code, s2 = call("api/siyanqaba/summary?id=" + urllib.parse.quote(sess["id"]))
check(code == 200 and s2["source"] == "plain" and s2["problems"], "Zerver unreachable: the plain order, said why")
code, sj = call("api/siyanqaba/session?id=" + urllib.parse.quote(sess["id"]))
check(sj["zerver"]["sent"] and sj["ai_set"], "the page shows it reached the Zerver")

srv.shutdown()
print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
