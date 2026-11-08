"""How much of the PC Debrief's live loops use (2.26), so we can prove they never slow iRacing down.

    m = PerfMeter("coach")
    with m.step():          # around one loop step (read + work), on the loop's own thread
        ...
    m.summary()  -> {"cpu_pct": 0.2, "step_ms": 0.4, "step_ms_max": 3.1, "slow_steps": 0, "steps": 1200}

cpu_pct is the loop thread's own CPU time over wall time in the last WINDOW_S seconds, as a share of ONE core
(the 5600X3D has 12 threads, so 1% of one core is under 0.1% of the whole CPU). A step slower than SLOW_MS is counted
and reported to the log at most once a minute (with on_slow), so a stall on the real PC explains itself.

memory_mb(): this process's resident memory (Windows working set or Linux RSS), None when unknown.
Pure Python, no imports beyond the standard library; safe on any OS.
"""
from __future__ import annotations

import os
import time
from collections import deque
from contextlib import contextmanager

WINDOW_S = 30.0
SLOW_MS = 50.0          # one coach step at 20 Hz has 50 ms; slower than that and samples are being skipped
LOG_EVERY_S = 60.0


class PerfMeter:
    def __init__(self, name: str, on_slow=None, wall=time.perf_counter, cpu=time.thread_time):
        self.name = name
        self._wall, self._cpu = wall, cpu
        self._on_slow = on_slow
        self._marks = deque()           # (wall, cpu) at the start of each step, last WINDOW_S seconds
        self._durs = deque(maxlen=600)  # recent step lengths, seconds
        self.steps = 0
        self.slow_steps = 0
        self.max_s = 0.0
        self._last_log = None

    @contextmanager
    def step(self):
        w0, c0 = self._wall(), self._cpu()
        try:
            yield
        finally:
            self._done(w0, c0)

    def _done(self, w0: float, c0: float):
        w1 = self._wall()
        d = max(0.0, w1 - w0)
        self.steps += 1
        self._durs.append(d)
        self.max_s = max(self.max_s, d)
        self._marks.append((w0, c0))
        while self._marks and w1 - self._marks[0][0] > WINDOW_S:
            self._marks.popleft()
        if d * 1000.0 > SLOW_MS:
            self.slow_steps += 1
            if self._on_slow is not None and (self._last_log is None or w1 - self._last_log >= LOG_EVERY_S):
                self._last_log = w1
                try:
                    self._on_slow(f"{self.name}: one step took {d * 1000.0:.0f} ms "
                                  f"({self.slow_steps} slow step{'s' if self.slow_steps != 1 else ''} so far)")
                except Exception:  # noqa: BLE001
                    pass

    def cpu_pct(self) -> float | None:
        if len(self._marks) < 2:
            return None
        (w0, c0) = self._marks[0]
        w1, c1 = self._wall(), self._cpu()
        if w1 - w0 <= 0:
            return None
        return max(0.0, (c1 - c0) / (w1 - w0) * 100.0)

    def summary(self) -> dict:
        durs = list(self._durs)
        avg = sum(durs) / len(durs) if durs else None
        pct = self.cpu_pct()
        return {"cpu_pct": None if pct is None else round(pct, 2),
                "step_ms": None if avg is None else round(avg * 1000.0, 2),
                "step_ms_max": round(self.max_s * 1000.0, 1), "slow_steps": self.slow_steps, "steps": self.steps}


def words(summary: dict | None) -> str:
    """One plain line for the window: 'Using about 0.2% of one CPU core (0.4 ms a step).'"""
    if not summary or summary.get("cpu_pct") is None:
        return ""
    pct = summary["cpu_pct"]
    share = "under 0.1%" if pct < 0.1 else f"about {pct:.1f}%"
    text = f"Using {share} of one CPU core ({summary.get('step_ms') or 0:.1f} ms a step)."
    if summary.get("slow_steps"):
        n = summary["slow_steps"]
        text += f" {n} slow step{'s' if n != 1 else ''} (over {SLOW_MS:.0f} ms): see debrief.log."
    return text


def memory_mb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            c = _PMC()
            c.cb = ctypes.sizeof(_PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            if not ctypes.windll.psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return round(c.WorkingSetSize / 1e6, 1)
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1e3, 1)
    except Exception:  # noqa: BLE001
        return None
    return None
