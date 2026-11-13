"""Debrief 2.35 review fixes: rebuilding a PC review of a race with no iRacing session number rebuilds THAT race.

    python3 tests/ci_run.py tests/test_235_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-235f-"))
LIB, TEL = TMP / "lib", TMP / "telemetry"
LIB.mkdir()
TEL.mkdir()
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"),
                  DEBRIEF_NO_NETWORK="1")
(TMP / "settings.toml").write_text(f"library_folder = '{LIB.as_posix()}'\niracing_folder = '{TMP.as_posix()}'\n"
                                   "sync_on_open = false\n", encoding="utf-8")

import fake_ibt as F  # noqa: E402
from debrief import app, localrace  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

app.LOG_FILE = str(TMP / "debrief.log")


def test_rebuild_no_subsession_race_picks_the_right_race():
    old = TEL / "mclaren570sgt4_zolder gp 2026-11-08 20-00-00.ibt"
    new = TEL / "mclaren570sgt4_zolder gp 2026-11-10 20-00-00.ibt"
    F.make(old, [99.4, 98.8, 98.6, 98.9, 98.7, 99.0], session="Race", start_date=1_794_100_000)
    F.make(new, [97.4, 97.8, 97.6, 97.9, 97.7, 98.0], session="Race", start_date=1_794_340_000)
    rs = localrace.races(str(TEL))
    assert len(rs) == 2 and all(not r.get("subsession") for r in rs), rs
    cfg = app.load_settings()
    older = rs[1]
    eid = localrace.event_id(older)
    assert eid != localrace.event_id(rs[0])
    page = app.cmd_review_local(cfg, session=older, open_after=False)
    d = page.parent
    params = S.review_rebuild_params(d)
    assert params["local"] and params["rebuild"] and params["pc_event"] == eid and params["subsession"] is None, params
    before = json.loads((d / "review.json").read_text(encoding="utf-8"))["meta"]["event_id"]
    page2 = app.cmd_review_local(cfg, open_after=False, rebuild=True, event_id=params["pc_event"])
    assert page2.parent == d, (page2, d)
    assert json.loads((d / "review.json").read_text(encoding="utf-8"))["meta"]["event_id"] == before
    # the file is gone: say so plainly, leave the review as it is
    mtime = (d / "review.json").stat().st_mtime_ns
    try:
        app.cmd_review_local(cfg, open_after=False, rebuild=True, event_id="irdeadbeef00")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "isn't on this PC" in str(e)
    assert (d / "review.json").stat().st_mtime_ns == mtime


def test_pc_event_is_validated():
    assert S._pc_event("irabc123") == "irabc123"
    assert S._pc_event("../x") is None and S._pc_event(None) is None


if __name__ == "__main__":
    import shutil
    import traceback
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
