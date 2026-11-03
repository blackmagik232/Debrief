"""2.14: first-run walkthrough logic (firstrun.py). Run: python tests/test_214_firstrun.py

Settings, Setup Lab's index and the state file all live in a temp folder (DEBRIEF_SETTINGS, DEBRIEF_CACHE_DIR)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-214-first-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import firstrun as fr, setuplab  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def by_id(w):
    return {s["id"]: s for s in w["steps"]}


print("== steps from facts")
st = fr.steps({})
check([s["id"] for s in st] == list(fr.STEP_IDS) and not any(s["done"] for s in st), "five steps, none done")
check(all(s["action"] for s in st) and all(s["title"] and s["text"] for s in st), "each has a title, text and action")
txt = json.dumps(st, ensure_ascii=False)
check("\u2014" not in txt and "\u2013" not in txt, "no long dashes")
st = {s["id"]: s for s in fr.steps({"token": True, "folder": True, "folder_path": "C:/x", "files": 3,
                                     "best_clean": 4, "voice_tested": True})}
check(st["token"]["done"] and st["token"]["action"] is None, "token done: no action")
check(st["telemetry"]["done"] and "3 telemetry files read" in st["telemetry"]["text"], st["telemetry"]["text"])
check(not st["session"]["done"] and "4 clean laps" in st["session"]["text"] and st["session"]["action"] == "practice",
      st["session"]["text"])
st = {s["id"]: s for s in fr.steps({"folder": True, "files": 0})}
check("Alt+L" in st["telemetry"]["text"], "no files yet: says how to turn telemetry on")
w = fr.walkthrough({"token": True, "folder": True, "files": 3, "best_clean": 6, "voice_tested": True}, state={})
check(w["all_done"] and not w["show"] and w["text"] == "You're all set.", "all done: not shown")
w = fr.walkthrough({"token": True}, state={"dismissed": True})
check(w["dismissed"] and not w["show"] and w["next"] == "folder" and w["done"] == 1, "dismissed: not shown")
w = fr.walkthrough({"token": True}, state={})
check(w["show"] and w["text"] == "1 of 5 done. Next: Find your iRacing telemetry.", w["text"])
check(fr.steps(None)[0]["done"] is False and fr.steps("junk")[0]["id"] == "token", "bad facts: no crash")

print("== gather from Debrief's own files")
SET = Path(os.environ["DEBRIEF_SETTINGS"])
f = fr.gather()
check(f["token"] is False and f["files"] == 0 and f["voice_tested"] is False, "fresh install: nothing done")
TEL = TMP / "iRacing" / "telemetry"
TEL.mkdir(parents=True)
SET.write_text('token = "g61-test-token-ABCDEFGHIJKLMNOP"\n' + f"iracing_folder = '{TMP / 'iRacing'}'\n",
               encoding="utf-8")
f = fr.gather()
check(f["token"] and f["folder"] and Path(f["folder_path"]) == TEL, f"token and folder found ({f['folder_path']})")
check(f["files"] == 0 and f["best_clean"] == 0, "no index yet: nothing read")
cl.make(TEL / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt", 3)
cache = TMP / "cache" / fr.SETUPLAB_CACHE
setuplab.index(TEL, cache_path=cache)
f = fr.gather()
check(f["files"] == 1 and 0 < f["best_clean"] < fr.MIN_CLEAN, f"one short session read ({f['best_clean']} clean laps)")
cl.make(TEL / "mclaren570sgt4_zolder gp 2026-11-02 10-00-00.ibt", 9)
setuplab.index(TEL, cache_path=cache)
f = fr.gather()
check(f["files"] == 2 and f["best_clean"] >= fr.MIN_CLEAN, f"a session with {f['best_clean']} clean laps")
w = fr.walkthrough()
check(w["next"] == "voice" and w["done"] == 4 and w["show"], f"only the voice left: {w['text']}")

print("== the state file")
check(fr.mark("voice_tested"), "voice tested remembered")
check(fr.state_path().parent == SET.parent and fr.state_path().name == "firstrun.json", "next to settings.toml")
w = fr.walkthrough()
check(w["all_done"] and not w["show"], "everything done")
check(fr.dismiss() and fr.load_state()["dismissed"] is True and fr.load_state()["voice_tested"] is True,
      "dismissed kept beside the voice flag")
check(fr.undismiss() and fr.load_state()["dismissed"] is False, "undismissed")
check(not fr.mark("token"), "only known flags can be set")
fr.state_path().write_text("{broken", encoding="utf-8")
check(fr.load_state() == {} and fr.gather()["voice_tested"] is False, "damaged state file: counts as empty")
check(fr.dismiss() and fr.load_state() == {"dismissed": True}, "and is replaced on the next save")
fr.state_path().write_text("[1, 2]", encoding="utf-8")
check(fr.load_state() == {}, "not a dict: empty")
cache.write_text("{junk", encoding="utf-8")
f = fr.gather()
check(f["files"] == 0 and f["best_clean"] == 0, "damaged index: nothing read (no crash)")
cache.write_text(json.dumps({"version": -1, "files": {}}), encoding="utf-8")
check(fr.gather()["files"] == 0, "old index version: ignored")
SET.write_text("this is = not [valid toml", encoding="utf-8")
f = fr.gather()
check(f["token"] is False, "damaged settings: token not counted (no crash)")
own = TMP / "own"
check(fr.mark("voice_tested", path=own / "fr.json") and fr.load_state(own / "fr.json") == {"voice_tested": True},
      "a given state path works")
f = fr.gather(raw_settings={}, cache_file=TMP / "none.json", state_path=own / "fr.json", folder=TMP / "missing")
check(f["voice_tested"] and not f["folder"] and f["files"] == 0, "explicit inputs")

# 2.14 review N7: two flags set at the same moment both stay set
import threading  # noqa: E402

lost = 0
for k in range(30):
    sp = TMP / "race" / f"fr{k}.json"
    a = threading.Thread(target=fr.mark, args=("voice_tested",), kwargs={"path": sp})
    b = threading.Thread(target=fr.mark, args=("dismissed",), kwargs={"path": sp})
    a.start()
    b.start()
    a.join()
    b.join()
    st_ = fr.load_state(sp)
    lost += not (st_.get("voice_tested") and st_.get("dismissed"))
check(lost == 0, f"marks made at the same moment are both kept ({lost} of 30 lost)")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
