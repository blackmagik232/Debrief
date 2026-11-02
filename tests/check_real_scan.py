"""Check the replay scans in a Debrief library against what the review pages will say. Read-only.

    python tests/check_real_scan.py "<library folder>"
    python tests/check_real_scan.py "<library folder>" --json

For every review with a scan it prints: lap mapping coverage, class data, finish overall and in class, on-track
passes, lap states, pace peers, and a list of anything that looks wrong. A review saved by an older Debrief is also
reworked in memory from its saved scan, exactly as a library refresh would, and that result is what gets checked;
nothing is written to the folder. Not a test: tests/run_all.py only runs test_*.py. Exit code 2 means no reviews folder.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import racecap_link, replay_scan  # noqa: E402

MIN_COVERAGE = 0.7          # racecap link's own rule (replay_scan.laps_agree): 70% of review laps must match the scan
MAX_UNKNOWN_STATES = 0.3
MAX_PASSES_PER_LAP = 2.0
MAX_PEER_GAP_S = 5.0
MAX_PACE_GAP_S = 1.0
BIG_LAP1_SHARE = 0.25       # a change of a quarter of the field on lap 1 is possible, but worth a look


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _rows(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def lap_mapping(rc: dict, data: dict) -> dict:
    review_n = len(_rows(data.get("laps")))
    lap_map = rc.get("lap_map") if isinstance(rc.get("lap_map"), dict) else {}
    pairs = sorted((int(k), v) for k, v in lap_map.items() if str(k).isdigit() and _int(v) is not None)
    return {"review_laps": review_n, "mapped": len(pairs), "coverage": round(len(pairs) / review_n, 2) if review_n else None,
            "in_order": all(a[1] < b[1] for a, b in zip(pairs, pairs[1:])), "pairs": pairs}


def class_data(rc: dict, scan: dict | None) -> dict:
    cars = _rows(scan.get("cars")) if scan else []
    classes = {c.get("car_class") for c in cars if c.get("car_class") is not None}
    return {"scan_cars_with_class": sum(1 for c in cars if c.get("car_class") is not None), "scan_cars": len(cars),
            "classes": len(classes), "my_class": rc.get("car_class_name"), "class_size": rc.get("class_size"),
            "present": rc.get("class_finish") is not None or bool(classes)}


def pass_counts(rc: dict) -> dict:
    on = [p for p in _rows(rc.get("passes")) if p.get("how") == "on track" and (_int(p.get("lap")) or 0) > 1]
    made = [p for p in on if p.get("made")]
    return {"all_rows": len(_rows(rc.get("passes"))), "made": len(made), "suffered": len(on) - len(made),
            "stuck": sum(1 for p in made if p.get("stuck"))}


def lap_states(rc: dict) -> dict:
    out = {}
    for r in _rows(rc.get("laps")):
        k = str(r.get("state") or "none")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def suspicious(rc: dict, data: dict, scan: dict | None, reworked: bool, had_samples: bool) -> list:
    """Plain sentences. Each one is a thing to look at, not proof of a bug."""
    out = []
    say = out.append
    version = _int(rc.get("version")) or 1
    if version < 2:
        say("Still the old scan format, and it can't be reworked" + ("" if had_samples else " (no samples file beside the scan)") + ": it won't count in Racecraft.")
    if reworked:
        say("The saved review is the old scan format. These numbers are what the next library refresh should write, not what the page shows today.")
    m = lap_mapping(rc, data)
    if m["coverage"] is None:
        say("The review has no laps to map.")
    elif m["coverage"] < MIN_COVERAGE:
        say(f"Only {m['mapped']} of {m['review_laps']} review laps map to a race lap.")
    if not m["in_order"]:
        say("Lap mapping goes backwards: review laps and race laps aren't in the same order.")
    cars = _int(rc.get("cars"))
    finish, cf, cs = _int(rc.get("finish")), _int(rc.get("class_finish")), _int(rc.get("class_size"))
    if finish is None:
        say("No finishing position.")
    elif cars and not 1 <= finish <= cars:
        say(f"Finish P{finish} is outside the {cars} cars in the scan.")
    if cf is not None and finish is not None and cf > finish:
        say(f"P{cf} in class is worse than P{finish} overall, which can't be.")
    if cf is not None and cs and cf > cs:
        say(f"P{cf} in class, but the class has {cs} cars.")
    c = class_data(rc, scan)
    if not c["present"]:
        say("No class data, so pace is compared with similar cars and there is no class finish." + (" Scan again with this version." if scan else ""))
    if c["scan_cars"] and 0 < c["scan_cars_with_class"] < c["scan_cars"]:
        say(f"Only {c['scan_cars_with_class']} of {c['scan_cars']} cars have a class.")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if c["classes"] > 1 and not meta.get("multiclass"):
        say(f"The scan shows {c['classes']} classes, but the review says one class.")
    if c["classes"] == 1 and meta.get("multiclass"):
        say("The review says multiclass, but the scan shows one class.")
    if _int(rc.get("grid")) is None:
        say("No starting position: the scan couldn't read every car at the green, and there are no official results.")
    laps = _rows(rc.get("laps"))
    positions = [x for x in _rows(rc.get("positions")) if _int(x.get("pos"))]
    if cars and any(not 1 <= x["pos"] <= cars for x in positions):
        say("A lap position is outside the field.")
    if laps and len(positions) < 0.5 * len(laps):
        say(f"Only {len(positions)} of {len(laps)} laps have a position.")
    if positions and finish and not rc.get("ended") and abs(positions[-1]["pos"] - finish) > 3:
        say(f"Last position on the chart is P{positions[-1]['pos']} but the finish is P{finish}.")
    g, a1 = _int(rc.get("grid")), _int(rc.get("pos_after_lap1"))
    if g and a1 and cars and abs(g - a1) >= max(3, BIG_LAP1_SHARE * cars):
        say(f"P{g} to P{a1} on lap 1 is a big swing.")
    p = pass_counts(rc)
    if laps and (p["made"] + p["suffered"]) / len(laps) > MAX_PASSES_PER_LAP:
        say(f"{p['made'] + p['suffered']} on-track passes in {len(laps)} laps is a lot: some may be readings, not passes.")
    if len(laps) >= 5 and not p["made"] and not p["suffered"]:
        say("No on-track passes at all over the race.")
    craft = rc.get("craft") if isinstance(rc.get("craft"), dict) else None
    if craft and (craft.get("made") != p["made"] or craft.get("lost") != p["suffered"]):
        say("The saved pass totals don't match the pass list.")
    st = lap_states(rc)
    if laps:
        if st.get("unknown", 0) + st.get("none", 0) > MAX_UNKNOWN_STATES * len(laps):
            say(f"{st.get('unknown', 0) + st.get('none', 0)} of {len(laps)} laps have no known state.")
        if not st.get("clear") and len(laps) >= 8:
            say("Not one lap in clear air: pace in clear air can't be worked out.")
        if len({r.get("lap") for r in laps}) != len(laps):
            say("The same race lap appears twice.")
    else:
        say("No lap-by-lap rows.")
    pace = rc.get("pace") if isinstance(rc.get("pace"), dict) else {}
    peers = _int(pace.get("peers"))
    if not peers:
        say("No peers to compare pace with.")
    else:
        if cars and peers > cars - 1:
            say(f"{peers} peers, but only {cars} cars in the race.")
        if _int(pace.get("peer_rank")) and pace["peer_rank"] > peers + 1:
            say(f"Pace rank {pace['peer_rank']} is outside the {peers + 1} cars compared.")
        if _num(pace.get("vs_peers")) is not None and abs(pace["vs_peers"]) > MAX_PEER_GAP_S:
            say(f"{pace['vs_peers']:+.2f} s against peers is a big gap: check they are the right cars.")
        if c["present"] and c["scan_cars_with_class"] and pace.get("peer_label") != "your class":
            say("The scan has classes, but pace peers aren't your class.")
    tl, med = _num(pace.get("typical")), _num(data.get("median"))
    if tl and med and abs(tl - med) > MAX_PACE_GAP_S:
        say(f"Typical lap from the scan and the review differ by {abs(tl - med):.2f} s.")
    return out


def check_review(library: Path, review_dir: Path) -> dict | None:
    """One review's report, or None when it has no scan at all."""
    data = json.loads((review_dir / "review.json").read_text(encoding="utf-8"))
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    own = replay_scan.find_for_event(library, meta.get("event_id"))
    stored = data.get("racecap") if isinstance(data.get("racecap"), dict) else None
    if stored is None and own is None:
        return None
    scan = racecap_link.read_scan(own) if own else None
    work = copy.deepcopy(data)      # refresh_saved changes the dict it is given, and only that
    had_samples = bool(own and racecap_link.read_samples(own) is not None)
    stored_v = (_int(stored.get("version")) or 1) if stored else 0
    reworked = stored_v < 2 and racecap_link.refresh_saved(library, work)
    rc = work.get("racecap") if isinstance(work.get("racecap"), dict) else {}
    m = lap_mapping(rc, work)
    return {"review": review_dir.name, "race": rc.get("subsession"), "stored_version": stored_v, "checked_version": _int(rc.get("version")) or 1,
            "reworked_in_memory": bool(reworked), "scan_found": own is not None,
            "laps": {k: m[k] for k in ("review_laps", "mapped", "coverage", "in_order")}, "class": class_data(rc, scan),
            "finish": {"overall": rc.get("finish"), "in_class": rc.get("class_finish"), "cars": rc.get("cars"), "official": bool(rc.get("official")), "grid": rc.get("grid"), "after_lap1": rc.get("pos_after_lap1")},
            "passes": pass_counts(rc), "states": lap_states(rc),
            "peers": {k: (rc.get("pace") or {}).get(k) for k in ("peers", "peer_label", "peer_rank", "vs_peers", "typical", "peer_typical")},
            "suspicious": suspicious(rc, work, scan, bool(reworked), had_samples)}


def check_library(library) -> dict:
    library = Path(library)
    out, without, broken = [], [], []
    for d in sorted((library / "reviews").iterdir()):
        if not (d / "review.json").is_file():
            continue
        try:
            one = check_review(library, d)
        except (OSError, ValueError) as e:
            broken.append({"review": d.name, "why": type(e).__name__})
            continue
        (out if one else without).append(one or d.name)
    seen = {}
    for r in out:
        if r["race"]:
            seen.setdefault(str(r["race"]), []).append(r["review"])
    for r in out:
        if r["race"] and len(seen[str(r["race"])]) > 1:
            r["suspicious"].append(f"Race {r['race']} has {len(seen[str(r['race'])])} reviews: Racecraft counts only the newest.")
    # scans that nothing points to
    linked = set()
    for r in out:
        linked.add(str(r["race"]))
    orphans = []
    root = replay_scan.scans_root(library)
    try:
        for f in sorted(root.iterdir()):
            sub = f.name.split("_")[0]
            if f.is_dir() and sub not in linked:
                orphans.append(f.name)
    except OSError:
        pass
    return {"library": str(library), "reviews": out, "without_scan": without, "unreadable": broken, "scans_without_review": orphans}


def text_report(rep: dict) -> str:
    L = [f"Library: {rep['library']}", f"{len(rep['reviews'])} scanned, {len(rep['without_scan'])} without a scan, {len(rep['unreadable'])} unreadable.", ""]
    for r in rep["reviews"]:
        a, c, f, p, e = r["laps"], r["class"], r["finish"], r["passes"], r["peers"]
        cov = "n/a" if a["coverage"] is None else f"{a['coverage']:.0%}"
        L += [r["review"], f"  Race {r['race']}, scan format saved {r['stored_version']}, checked {r['checked_version']}" + (" (reworked in memory)" if r["reworked_in_memory"] else ""),
              f"  Lap mapping: {a['mapped']} of {a['review_laps']} review laps ({cov})",
              f"  Class data: {'yes' if c['present'] else 'no'} ({c['scan_cars_with_class']} of {c['scan_cars']} cars, {c['classes']} classes)",
              f"  Finish: overall {f['overall']}, in class {f['in_class']}, of {f['cars']} cars, " + ("official" if f["official"] else "on the road") + f", grid {f['grid']}, after lap 1 {f['after_lap1']}",
              f"  On-track passes after lap 1: {p['made']} made, {p['suffered']} suffered, {p['stuck']} stuck ({p['all_rows']} pass rows in all)",
              "  Lap states: " + (", ".join(f"{k} {v}" for k, v in r["states"].items()) or "none"),
              f"  Peers: {e['peers']} ({e['peer_label']}), your rank {e['peer_rank']}, {e['vs_peers']} s against them",
              "  Suspicious:" if r["suspicious"] else "  Suspicious: nothing"]
        L += [f"    - {s}" for s in r["suspicious"]]
        L.append("")
    if rep["without_scan"]:
        L += ["No scan: " + ", ".join(rep["without_scan"]), ""]
    for b in rep["unreadable"]:
        L.append(f"Unreadable: {b['review']} ({b['why']})")
    if rep["scans_without_review"]:
        L.append("Scans with no review: " + ", ".join(rep["scans_without_review"]))
    return "\n".join(L).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only check of the scanned reviews in a Debrief library.")
    ap.add_argument("library", help="the library folder (it holds reviews\\)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    a = ap.parse_args(argv)
    if not (Path(a.library) / "reviews").is_dir():
        print(f"No reviews folder in {a.library}", file=sys.stderr)
        return 2
    rep = check_library(a.library)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(rep, indent=1, ensure_ascii=False) if a.json else text_report(rep), end="\n" if a.json else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
