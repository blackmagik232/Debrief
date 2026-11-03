"""Stress test for the live coach (Coach 2.0): play telemetry files through the coach as fast as possible, timing
every cue with a voice pack's exact clip lengths, and count what a driver would hear.

    python tools/coach_stress.py <telemetry folder> [--pack engineer] [--top 3]

For each car and track: the reference is the best clean lap across that pair's files; the focus corners are the
reference's slowest braked corners (top N, as if the reviews named them). Every file is then played through the
coach. Reported: cues per lap (the most and the average), the smallest gap between a cue's end and the next brake
point, cues ending less than Settings.end_before_brake_s before a brake point, and cues that would overlap (one
starts before the last one ended, audio delay included). Files are only read.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from debrief import ibt, livecoach as lc, voicepack as vp  # noqa: E402


def key_of(path: Path) -> str:
    """'mclaren570sgt4_zolder gp 2026-...' -> 'mclaren570sgt4_zolder gp' (iRacing's own file naming)."""
    parts = path.stem.rsplit(" ", 2)
    return parts[0] if len(parts) == 3 else path.stem


def run(folder: Path, pack_id: str, top: int) -> dict:
    pack = vp.find_pack(pack_id)
    if pack is None:
        raise SystemExit(f"voice pack {pack_id} not found")

    def dur(text):
        keys = pack.compose(text)
        return pack.duration_ms(keys) / 1000.0 if keys else len(text.split()) / 2.6

    groups = collections.defaultdict(list)
    for p in sorted(folder.glob("*.ibt")):
        groups[key_of(p)].append(p)
    st = lc.Settings(top_n=top)
    total = {"files": 0, "laps": 0, "cues": 0, "max_per_lap": 0, "short": 0, "overlaps": 0, "min_gap_s": None,
             "uncovered": set(), "pairs": []}
    for key, paths in groups.items():
        ref = lc.build_reference(paths)
        if ref is None:
            print(f"{key}: no clean lap for a reference, skipped")
            continue
        braked = sorted(ref.braked, key=lambda c: c.min_kmh)[:top]
        weak = [{"id": c.id, "apex_m": c.apex_m, "avg_avail": 0.3} for c in braked]
        per_lap = collections.Counter()
        pair = {"pair": key, "files": len(paths), "laps": 0, "cues": 0, "short": 0, "overlaps": 0}
        for p in paths:
            events = []
            coach = lc.Coach(ref, weak, st, duration=dur)
            coach.on_event = events.append
            src = lc.IbtPlaybackSource(p, speed=0)
            end_t = None
            laps = set()
            while True:
                smp = src.read()
                if smp is None:
                    break
                laps.add((p.name, smp.get("Lap")))
                for cue in coach.feed(smp):
                    d = dur(cue.text)
                    if pack.compose(cue.text) is None:
                        total["uncovered"].add(cue.text)
                    t0 = cue.spoken_t + 0.0
                    if end_t is not None and t0 < end_t - 1e-6:
                        pair["overlaps"] += 1
                    end_t = t0 + d + st.audio_latency_s
            for e in events:
                if e.get("ev") != "spoken":
                    continue
                per_lap[(p.name, e.get("lap"))] += 1
                pair["cues"] += 1
                nb = e.get("next_brake_s")
                if nb is not None:
                    gap = nb - e["dur_s"] - st.audio_latency_s
                    total["min_gap_s"] = gap if total["min_gap_s"] is None else min(total["min_gap_s"], gap)
                    if gap < st.end_before_brake_s - 1e-6:
                        pair["short"] += 1
            pair["laps"] += len(laps)
        pair["max_per_lap"] = max(per_lap.values(), default=0)
        total["pairs"].append(pair)
        for k in ("laps", "cues", "short", "overlaps"):
            total[k] += pair[k]
        total["files"] += pair["files"]
        total["max_per_lap"] = max(total["max_per_lap"], pair["max_per_lap"])
        print(f"{key}: {pair['files']} files, {pair['laps']} laps, {pair['cues']} cues, at most "
              f"{pair['max_per_lap']} a lap, {pair['short']} ending < {st.end_before_brake_s:g} s before a brake "
              f"point, {pair['overlaps']} overlapping")
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder")
    ap.add_argument("--pack", default="engineer")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args(argv)
    t = run(Path(a.folder), a.pack, a.top)
    avg = t["cues"] / t["laps"] if t["laps"] else 0
    gap = "n/a" if t["min_gap_s"] is None else f"{t['min_gap_s']:.2f} s"
    print(f"TOTAL: {t['files']} files, {t['laps']} laps, {t['cues']} cues ({avg:.2f} a lap), at most "
          f"{t['max_per_lap']} in one lap; smallest gap to the next brake point {gap}; {t['short']} too close; "
          f"{t['overlaps']} overlapping; {len(t['uncovered'])} lines not in the pack {sorted(t['uncovered'])[:5]}")
    return 0 if not t["short"] and not t["overlaps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
