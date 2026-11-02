"""A stand-in for iRacing: a scripted 13-car race for testing the replay scan (from RaceCap)."""
import math, os
UI_HIDDEN = 8

# A scripted race for the scan: who starts where, how fast each car laps, and what goes wrong.
GREEN = 122.0
GRID = [3, 5, 7, 6, 1, 2, 4, 8, 9, 10, 11, 12, 13]                    # car index, pole first
LAP = {3: 80.0, 5: 79.6, 7: 81.0, 6: 81.0, 1: 82.0, 2: 82.5, 4: 83.0, 8: 83.5, 9: 84.0, 10: 84.5, 11: 85.0, 12: 85.5, 13: 86.0}
# (car index, from, to, share of normal pace, track surface, on pit road)
EVENTS = [(10, 300.0, 306.0, 0.3, 0, False),                           # #45 runs wide
          (7, 400.0, 404.0, 0.2, 0, False),                            # #15 and #93 go off together...
          (6, 401.0, 406.0, 0.2, 0, False), (6, 406.0, 420.0, 0.0, 3, False),   # ...and #93 sits stopped on track
          (1, 500.0, 530.0, 0.3, 2, True)]                             # #11 pits
RETIRE = {13: 450.0}                                                   # #099 leaves the session
RACE_LAPS = 6


class FakeClock:
    def __init__(self):
        self.t, self.parts = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for p in self.parts:
            p.advance(s)


class FakeSim:
    """A 13-car race. Session 0 practice, 1 qualify, 2 race. Lead changes at the times in `lead`."""
    def __init__(self, clock, end=600.0, lead=((0, 3), (200, 5), (380, 3))):
        clock.parts.append(self)
        self.end, self.lead = end, lead
        self.session_num, self.t, self.speed, self.cam_state = 0, 30.0, 1, 0
        self.car, self.group = 1, 1
        self.pending = None
        self.calls = []

    def advance(self, s):
        if self.pending:
            self.pending[2] -= s
            if self.pending[2] <= 0:
                self.session_num, self.t = self.pending[0], self.pending[1]
                self.pending = None
        self.t = min(self.t + s * self.speed, self.end)

    def connect(self):
        return True

    def info(self):
        names = ["Pace Car", "A Able", "B Baker", "M O'Dell", "C Choate", "E Sezgin", "F Meyn", "G Johnson", "H Spence",
                 "I Young", "J Williams", "K Boswell", "L Langenfeld", "N McKinney"]
        nums = ["0", "11", "12", "58", "17", "54", "93", "15", "46", "92", "45", "67", "91", "099"]
        return {"subsession": 89056204, "track": "Test Track", "track_config": "North", "track_key": "test-track-north", "track_length_m": 4000.0,
                "drivers": {i: {"number": nums[i], "name": names[i], "team": "", "pace_car": i == 0, "spectator": False} for i in range(14)},
                "groups": {1: {"name": "TV1", "cameras": {1: "CamA", 2: "CamB"}}, 2: {"name": "Chase", "cameras": {1: "Chase"}},
                           3: {"name": "Cockpit", "cameras": {1: "Cockpit"}}, 4: {"name": "Chopper", "cameras": {1: "Chopper"}}},
                "sessions": [{"num": 0, "type": "Practice", "name": "PRACTICE"}, {"num": 1, "type": "Lone Qualify", "name": "QUALIFY"},
                             {"num": 2, "type": "Race", "name": "RACE"}]}

    def leader(self):
        cur = self.lead[0][1]
        for t, idx in self.lead:
            if self.t >= t:
                cur = idx
        return cur

    def cam_now(self):
        """The live camera inside the group. An exact camera that was asked for is kept (hold_exact = 'yes'), kept only
        while auto shot selection is off ('auto off'), or dropped after 2 s ('no'), to play each way iRacing might behave."""
        auto = 1 + int(self.t // 7) % 2 if self.group == 1 else 1
        fixed = getattr(self, "fixed", None)
        if not fixed:
            return auto
        how = getattr(self, "hold_exact", "yes")
        if how == "yes" or (how == "auto off" and not self.cam_state & 0x0010):
            return fixed
        return fixed if self.t - getattr(self, "fixed_at", self.t) < 2.0 else auto

    def read(self):
        ldr = self.leader()
        order = [ldr] + [i for i in range(1, 14) if i != ldr]
        pos = [0] * 14
        for p, idx in enumerate(order, 1):
            pos[idx] = p
        moving = self.speed != 0 and self.t < self.end
        return {"session_num": self.session_num, "session_time": self.t, "frame": int(self.t * 60), "playing": moving,
                "speed": self.speed, "slowmo": False, "cam_car_idx": self.car, "cam_group": self.group,
                "cam_num": self.cam_now(), "cam_state": self.cam_state,
                "positions": pos if self.session_num == 2 else [0] * 14, "session_state": 4}

    # --- the scripted race (only the scan reads these)
    def progress(self, i, t):
        """Laps done plus share of the current lap; negative before the car first reaches the line."""
        t = min(t, RETIRE.get(i, t))
        start = -0.002 * (GRID.index(i) + 1)
        if t <= GREEN:
            return start
        run = t - GREEN
        for car, a, b, share, _, _ in EVENTS:
            if car == i:
                run -= (1 - share) * max(0.0, min(t, b) - max(GREEN, a))
        return start + run / LAP[i]

    def crossed(self, i, lap, upto):
        """Race time at which car i reached `lap` whole laps (searching up to `upto`)."""
        lo, hi = GREEN, upto
        for _ in range(40):
            mid = (lo + hi) / 2
            if self.progress(i, mid) < lap:
                lo = mid
            else:
                hi = mid
        return hi

    def chequered(self):
        if not hasattr(self, "_cheq"):
            self._cheq = min(self.crossed(i, RACE_LAPS, 5000.0) for i in GRID)
        return self._cheq

    def read_cars(self):
        t, n = self.t, 14
        out = {"session_num": self.session_num, "session_time": t, "playing": self.speed != 0 and t < self.end,
               "speed": self.speed, "flags": 0, "live_time": t,
               "session_state": 3 if t < GREEN else 4 if t < self.chequered() else 5}
        pct, surf, pit, lapc, key = [-1.0] * n, [-1] * n, [False] * n, [0] * n, {}
        if self.session_num == 2:
            for i in GRID:
                p = self.progress(i, t)
                done = math.floor(p)
                lapc[i] = max(done, 0)
                key[i] = (-done, self.crossed(i, done, t) if done >= 0 else GRID.index(i))
                if t >= RETIRE.get(i, 1e9):
                    continue
                pct[i], surf[i] = p % 1.0, 3
                for car, a, b, _, sf, pr in EVENTS:
                    if car == i and a <= t < b:
                        surf[i], pit[i] = sf, pr
        pos = [0] * n
        for place, i in enumerate(sorted(key, key=lambda i: key[i]), 1):
            pos[i] = place
        out.update(pct=pct, surface=surf, pit=pit, pos=pos, lapc=lapc, car_flags=[0] * n)
        return out

    PLAYER = 3                                  # the stand-in's own car: iRacing gives its pedals in a replay

    def read_lap(self, i):
        """One car during a lap pass, as the real sim gives it (see sim.read_lap)."""
        t = self.t
        p = self.progress(i, t) if self.session_num == 2 else -1.0
        pct = p % 1.0 if p >= 0 else -1.0
        mine = self.car == i
        corner = math.sin(pct * 2 * math.pi * 3)                 # three corners a lap
        return {"session_num": self.session_num, "session_time": t, "cam_car_idx": self.car, "player_car_idx": self.PLAYER,
                "pct": pct, "steer": 0.3 * corner, "rpm": 6000 + 1500 * (1 - abs(corner)), "gear": 4 if abs(corner) < 0.5 else 3,
                "lapc": max(0, math.floor(p)), "speed": (40.0 + 20 * (1 - abs(corner))) if mine else None,
                "throttle": (1.0 - abs(corner)) if mine and i == self.PLAYER else None,
                "brake": max(0.0, abs(corner) - 0.7) if mine and i == self.PLAYER else None}

    def seek(self, session_num, t):
        self.calls.append(("seek", session_num, t)); self.pending = [session_num, t, 0.5]

    def set_speed(self, speed):
        self.speed = speed

    def cam_car(self, number, group, camera=0):
        self.calls.append(("cam_car", number, group) + ((camera,) if camera else ()))
        self.car = next(i for i, d in self.info()["drivers"].items() if d["number"] == number); self.group = group
        self.fixed, self.fixed_at = (camera or None), self.t

    def cam_pos(self, position, group, camera=0):
        self.calls.append(("cam_pos", position, group))
        self.car = self.read()["positions"].index(position); self.group = group
        self.fixed = camera or None

    def cam_focus(self, mode, group, camera=0):
        """iRacing's own director targets, as the stand-in understands them."""
        self.calls.append(("cam_focus", mode, group))
        self.car = {"leader": self.leader(), "exciting": 5, "incident": 9}[mode]; self.group = group
        self.fixed = None

    def set_cam_state(self, state):
        self.calls.append(("cam_state", state))
        self.cam_state = state

    def read_extra(self, keys):
        """Gear and revs change, lap times are steady, steering is empty, tyre compound is not offered."""
        out = {}
        for k in keys:
            if k in ("CarIdxGear", "CarIdxRPM"):
                out[k] = [0] + [3 + (int(self.t * 4) + i) % 3 for i in range(1, 14)]
            elif k in ("CarIdxBestLapTime", "CarIdxLastLapTime"):
                out[k] = [0.0] + [88.0 + i for i in range(1, 14)]
            elif k == "CarIdxTireCompound":
                out[k] = None
            else:
                out[k] = [0] * 14
        return out

    def set_ui_hidden(self, hidden):
        self.cam_state = UI_HIDDEN if hidden else 0
