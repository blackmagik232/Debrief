"""Run Setup Lab on a real telemetry folder and print what it found. NOT part of CI (run_all.py only runs test_*.py).

    python tests/check_real_ibt.py "C:\\Users\\<you>\\Documents\\iRacing\\telemetry" [--series open|fixed] [--cache FILE]

Reads the .ibt files only (never writes, moves or copies them). Without --cache nothing is written anywhere.
Never copy real telemetry into this repository: it is public.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import setuplab as sl  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--series", default=None, help="open or fixed (default: unknown)")
    ap.add_argument("--cache", default=None)
    a = ap.parse_args(argv)
    t = time.time()
    idx = sl.index(a.folder, cache_path=a.cache)
    print(f"Scanned {a.folder}: {len(idx['files'])} readable, {len(idx['bad'])} unreadable, read {idx['read']}, "
          f"from cache {idx['cached']}, {time.time() - t:.2f} s")
    for b in idx["bad"]:
        print(f"  unreadable: {b['file']} ({b['error']})")
    for c in sl.combos(idx):
        rep = sl.report(a.folder, c["car_path"], c["track_code"], series_setup=a.series, idx=idx)
        print(f"\n=== {rep['car']} at {rep['track']} ({rep['files']} files, series setup: {rep['series_setup']}"
              f"{' - ' + rep['series_setup_source'] if rep.get('series_setup_source') else ''})")
        for s in rep["setups"]:
            print(f"  {s['label']}: first {s['first']}, last {s['last']}, {len(s['files'])} file(s)")
        for r in rep["runs"]:
            print(f"  run {r['id']}: {r['setup']} {r['first']} - {r['last']}, {len(r['files'])} file(s), "
                  f"{r['clean_laps']} clean laps, sessions {', '.join(r['sessions']) or '-'}")
        for d in rep["diffs"]:
            print(f"  changes {d['from']} -> {d['to']}:")
            for ch in d["changes"]:
                print(f"    - {ch['text']} [{ch['kind']}]")
        cmp_ = rep["compare"] or {}
        for s in cmp_.get("setups") or []:
            p = s["pace"]
            print(f"  pace {s['label']}: {p['clean_laps']} counted laps, median {p['median']}, best {p['best']}, "
                  f"left out {p['left_out']}, track {p['surface_temp']} C {p['track_state']}, fuel avg {p['fuel']}")
        print(f"  verdict: {cmp_.get('verdict')}")
        sug = rep["suggestions"] or {}
        if sug:
            print(f"  tyre pit readings used: {sug.get('pit_readings')}, left out: {sug.get('readings_left_out')}")
        for it in sug.get("items") or []:
            star = " (TRY THIS NEXT)" if it.get("next") else ""
            print(f"  [{it['kind']}/{it['confidence']}]{star} {it['text']}")
            if it.get("data") and it["kind"] != "note":
                print(f"      data: {it['data']}")
        for n in rep["notes"]:
            print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
