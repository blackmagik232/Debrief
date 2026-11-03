"""Run the 2.13 session review on a real telemetry folder and print it. NOT part of CI (CI runs tests/test_*.py).

    python tests/check_real_session.py "C:\\Users\\<you>\\Documents\\iRacing\\telemetry" [--car mclaren570sgt4] [--all]

Reads the .ibt files only (never writes, moves or copies them; no cache is written).
Never copy real telemetry into this repository: it is public.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import sessionibt as si  # noqa: E402
from debrief import setuplab as sl  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--car", default=None, help="only sessions of this car path")
    ap.add_argument("--laps", action="store_true", help="print every lap")
    a = ap.parse_args(argv)
    t = time.time()
    idx = sl.index(a.folder)
    ss = si.sessions(idx)
    print(f"{len(ss)} sessions in {a.folder} ({time.time() - t:.2f} s)")
    for s in ss:
        print(f"  {s['date']}  {s['car']} @ {s['track']}  sub {s['subsession']}  {len(s['files'])} file(s)  "
              f"{'/'.join(s['session_types'])}  laps {s['laps']} valid {s['valid_laps']} clean {s['clean_laps']}  "
              f"best {s['best']}  median {s['median']}")
    done = []
    for s in reversed(ss):                                   # oldest first, so each has the earlier ones as history
        if a.car and s["car_path"] != a.car:
            continue
        t = time.time()
        biggest = max(Path(p).stat().st_size for p in s["paths"]) / 1e6
        rv = si.review(s, history=list(done), idx=idx)
        dt = time.time() - t
        done.append(rv)
        print(f"\n=== {rv['date']} {rv['car']} @ {rv['track']} | {', '.join(rv['session_types'])} | "
              f"{len(rv['files'])} file(s), largest {biggest:.0f} MB, reviewed in {dt:.2f} s")
        print("  " + rv["summary"].get("text", ""))
        if a.laps:
            for r in rv["laps"]:
                print(f"    {r['file'][-12:-4]} lap {r['lap']:>2} {r['session'][:6]:6} {r['time'] if r['time'] else '-':>8} "
                      f"{r['timed_by']:7} {'CLEAN' if r['clean'] else 'valid' if r['valid'] else '     '} "
                      f"{r['reason'] or ''} fuel {r['fuel_used']} {r.get('segments') and len(r['segments'])}")
        print("  consistency: " + rv["consistency"]["text"])
        print(f"  best possible ({rv['theoretical']['basis']}, {len(rv['theoretical']['segments'])} segments): "
              + rv["theoretical"]["text"])
        print("  spread: " + rv["segment_spread"]["text"])
        print("  fuel: " + rv["fuel"]["text"])
        print("  conditions: " + rv["conditions"]["text"])
        print("  setups: " + "; ".join(f"{x['label']} {x['fingerprint']}" for x in rv["setups"]))
        print("  history: " + ((rv["history"] or {}).get("text") or "-"))
        for n in rv["notes"]:
            print("  note: " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
