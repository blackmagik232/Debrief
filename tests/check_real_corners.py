"""Run the corner coach (2.13) on real .ibt files and print the cards. NOT part of CI (run_all.py only runs test_*.py).

    python tests/check_real_corners.py <telemetry folder or .ibt files> [--car mclaren570sgt4] [--track "zolder gp"]
                                        [--by-file] [--json]

Reads the .ibt files only (never writes, moves or copies them). Never copy real telemetry into this repository.
Default: every file of the car/track with the most files, analysed together (laps across files); --by-file also
analyses each file on its own.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import cornercoach as cc  # noqa: E402


def show(rep, title):
    print(f"\n=== {title}: {rep['message']}")
    if rep.get("off_pace"):
        print(f"  left out as off-pace: {rep['off_pace']}")
    for k in ("corners_from", "abs", "abs_onset_pct", "wheelspin", "lockup"):
        if rep.get("method", {}).get(k) is not None:
            print(f"  {k}: {rep['method'][k]}")
    for c in rep["cards"]:
        print(f"\n  [{c['confidence']}] {c['text']}")
        print(f"    Why: {c['why']}")
        print(f"    Voice: \"{c['voice']}\"   (rule {c['rule']}" + (f", then {c['secondary']}" if c['secondary'] else "") + ")")
    for u in rep["unclear"]:
        print(f"\n  {u['text']}")
    if rep.get("fine"):
        print("\n  Fine (typical lap within 0.05 s of your best): " + ", ".join(f["corner"] for f in rep["fine"]))
    for c in rep.get("reference_cards") or []:
        print(f"\n  [reference, {c['confidence']}] {c['text']}")
    if rep.get("summary"):
        print("\n  Braking habits:")
        for line in rep["summary"]["lines"]:
            print(f"    - {line}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--car", default=None)
    ap.add_argument("--track", default=None)
    ap.add_argument("--by-file", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    files = []
    for p in map(Path, a.paths):
        files += sorted(p.glob("*.ibt")) if p.is_dir() else [p]
    def combo(f):
        car, _, rest = f.name.partition("_")
        return car.lower(), rest.rsplit(" 20", 1)[0].lower()
    if a.car or a.track:
        files = [f for f in files if (not a.car or combo(f)[0] == a.car.lower())
                 and (not a.track or combo(f)[1] == a.track.lower())]
    else:
        top = Counter(combo(f) for f in files).most_common(1)
        files = [f for f in files if top and combo(f) == top[0][0]]
    if not files:
        print("no .ibt files")
        return 1
    t = time.time()
    rep = cc.analyze_files(files)
    print(f"{len(files)} file(s), {rep['laps']} clean laps used, {time.time() - t:.2f} s")
    for f in rep["info"].get("files", []):
        print(f"  {f['file']}: {f['clean_laps']} clean lap(s) {f['session']}")
    for f in rep["info"].get("skipped", []):
        print(f"  skipped {f['file']}: {f['why']}")
    show(rep, "all files together")
    if a.json:
        print(json.dumps(rep, indent=1)[:4000])
    if a.by_file:
        for f in files:
            show(cc.analyze_files([f]), f.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
