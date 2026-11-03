"""2.16 check on the owner's real files (not run by CI): the start report and the incident map, read only.

    python tests/check_real_racing.py [library folder] [telemetry folder]

Defaults are the uploaded copies used while building 2.16. Prints what each telemetry file holds (sessions, incident
steps, positions at the green and after lap 1), then both reports' plain words and numbers. Nothing is written.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("irsdk", None)

from debrief import incidentmap as im  # noqa: E402
from debrief import startreport as sr  # noqa: E402

LIB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/mnt/user-data/uploads/Debrief Reviews")
TEL = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/mnt/user-data/uploads/Documents/iRacing/telemetry")


def main():
    t0 = time.perf_counter()
    src = im.collect(library_folder=LIB, telemetry_folder=TEL)
    t1 = time.perf_counter()
    print(f"Found {len(src['scans'])} replay scan(s), {len(src['telemetry'])} telemetry file(s), {len(src['reviews'])} review(s) "
          f"in {t1 - t0:.2f} s. Corners: " + "; ".join(f"{k} from {v['from']}" for k, v in src["corners"].items()))
    for n in src["notes"]:
        print("  note:", n)
    print("\nTELEMETRY FILES")
    for f in sorted(src["telemetry"], key=lambda x: x["when"]):
        print(f"  {f['file']}: race {f['subsession']}, {f['track']}, driver {f['driver']!r} (car {f['me_idx']}), "
              f"{'multi-class' if f['multiclass'] else 'one class'}, spotter {'yes' if f['has_spotter'] else 'no'}")
        for S in f["sessions"]:
            steps = [e["points"] for e in S["events"]]
            extra = ""
            if S["kind"] == "race":
                extra = (f", green {S.get('green')}, at the green P{S.get('start_pos')} (class P{S.get('start_class_pos')}), "
                         f"after lap 1 P{S.get('lap1_pos')} (class P{S.get('lap1_class_pos')})")
            print(f"    session {S['num']} {S['type']} ({S['kind']}): count {S['inc_first']} to {S['inc_last']}, steps {steps}{extra}")
    print("  Step sizes seen:", im.telemetry_steps_seen(src["telemetry"]))

    t2 = time.perf_counter()
    m = im.build(src)
    t3 = time.perf_counter()
    print(f"\nINCIDENT MAP ({t3 - t2:.2f} s): {m['sessions']} sessions, {m['events']} incidents. {m['steps_note']}")
    for t in m["tracks"]:
        print(f"\n  {t['track']}: {t['sessions']} sessions ({t['races']} races), {t['events']} incidents, {t['points']} points, "
              f"by session {t['by_session']}, kinds {t['types']}, lap 1 {t['lap1']}, not recorded {t['not_recorded']}")
        for b in t["by_zone"]:
            if b["events"]:
                print(f"    {b['label']:<34} {b['events']} incidents, {b['points']} points, lap 1 {b['lap1']}, "
                      f"own {b['own']} contact {b['contact']} caught up {b['caught_up']}, in {b['sessions']} sessions")
        for r in t["list"]:
            near = ", ".join(f"{x['name']} {x['metres']} m" for x in r["near"]) or "-"
            print(f"    {r['session']:<44} lap {str(r['lap']):<3} {r['where']:<14} {r['what']:<28} {r['type']:<9} {r['sure']:<8} near: {near}")
            print(f"        {r['why']}")
        print("    patterns:", t["patterns"] or "none")
        for s in t["sentences"]:
            print("    >", s)

    t4 = time.perf_counter()
    rep = sr.build(src)
    t5 = time.perf_counter()
    print(f"\nSTART REPORT ({t5 - t4:.2f} s): {rep['races']} races")
    for r in rep["list"]:
        print(f"\n  race {r['subsession']} at {r['track']}: start P{r['start']} ({r['start_from']}), after lap 1 P{r['lap1_pos']} "
              f"({r['lap1_from']}), change {r['change']}, field {r['field']}, band {(r['band'] or {}).get('label')}, "
              f"contact {r['contact']}, sources {r['sources']}")
        for c in r["corners"]:
            print(f"    {c['label']:<28} gained {c['gained']} lost {c['lost']} (cars seen {c['cars_seen']}), about P{c.get('est_pos')}")
        for s in r["sentences"]:
            print("    >", s)
    for g in rep["tracks"] + ([rep["overall"]] if rep["overall"] else []):
        print(f"\n  {g['track']}: lap 1 {g['lap1']}, trend {g['trend']}")
        for c in g["corners"]:
            print(f"    {c['label']:<28} n {c['n']} gained {c['gained']} lost {c['lost']} total {c['total']} p {c['p']} pattern {c['pattern']}")
        for b in g["bands"]:
            print(f"    band {b['band']:<7} n {b['n']} avg {b['avg']} p {b['p']} pattern {b['pattern']}")
        for s in g["sentences"]:
            print("    >", s)
    print(f"\nTotal {time.perf_counter() - t0:.2f} s")


if __name__ == "__main__":
    main()
