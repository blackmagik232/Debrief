"""A scripted race for the crew chief tests (2.29): readings of your car and the CarIdx arrays, as livedata.LiveReader
gives them, but made up. Every car runs round at its own lap time; a caution slows everybody; the player (and any
other car) can be sent through the pits. No iRacing, no files, no network.

    sim = RaceSim(laps=50, tank=60.0, burn=3.0, lap_s=60.0)
    sim.pit_at(lap=15, add=48.0)                 # the player pits at the end of lap 15 and takes 48 L
    sim.caution(300.0, 420.0)                    # session seconds
    for own, field in sim.run(): ...             # one reading every sim.dt seconds

Units: litres, seconds, laps. Defaults give a 50 lap race on a 60 L tank at 3 L a lap: 2 stops (hand check in the tests).
"""
from __future__ import annotations

import math

CLASS_A, CLASS_B = 4029, 4030
ME = 2


class Car:
    def __init__(self, idx, lap_s, ahead_laps, cls=CLASS_A, num=None, name=None):
        self.idx, self.lap_s, self.cls = idx, lap_s, cls
        self.D = ahead_laps                  # distance driven, in laps (the start line is D = 0, 1, 2 ...)
        self.lap_start = 0.0
        self.last = 0.0
        self.pit = None                      # (from session s, to session s): on pit road
        self.num = num or str(idx + 10)
        self.name = name or f"Driver {idx}"
        self.completed = int(math.floor(self.D))

    @property
    def pct(self):
        return self.D - math.floor(self.D)


class RaceSim:
    def __init__(self, laps=50, tank=60.0, burn=3.0, lap_s=60.0, dt=0.1, cars=12, timed_s=None, start_fuel=None,
                 multiclass=False, f2=True, state=4, session_type="Race", reserve_fuel_pct=1.0):
        self.dt = dt
        self.laps, self.timed_s = laps, timed_s
        self.tank, self.burn, self.lap_s = tank, burn, lap_s
        self.t = 0.0
        self.fuel = tank if start_fuel is None else start_fuel
        self.f2, self.state, self.session_type = f2, state, session_type
        self.cars = {}
        self.multiclass = multiclass
        # the player is car ME; the others are spread round the grid 1.5 s apart (lower numbers are ahead of you)
        for i in range(cars):
            d = 0.05 + (ME - i) * 1.5 / lap_s
            self.cars[i] = Car(i, lap_s if i == ME else lap_s + 0.05 * (i % 3), d,
                               cls=(CLASS_B if multiclass and i % 2 else CLASS_A))
        self.cautions = []                   # (from, to) session seconds
        self.pits: list = []                 # player: dict(lap, add, stall, lane)
        self._pit_state = None
        self.fuel_rate = 5.0
        self.towed = []
        self.tow_until = -1.0
        self.brake = 0.0
        self.throttle = 1.0
        self.side = 1
        self.flags_extra = 0
        self.stopped = False
        self.freeze_from = None
        self.dead = False
        self.on_pit_cars: dict = {}

    # ------------------------------------------------------------ script
    def caution(self, a, b):
        self.cautions.append((a, b))

    def pit_at(self, lap, add, stall=12.0, lane_s=40.0):
        """The player drives into the pits at the end of this lap (a stop of `stall` seconds, `lane_s` on pit road)."""
        self.pits.append({"lap": lap, "add": add, "stall": stall, "lane": lane_s, "done": False})

    def car_pit(self, idx, t0, t1):
        self.cars[idx].pit = (t0, t1)

    def set_pace(self, idx, lap_s):
        self.cars[idx].lap_s = lap_s

    def in_caution(self, t=None):
        t = self.t if t is None else t
        return any(a <= t < b for a, b in self.cautions)

    # ------------------------------------------------------------ the world
    def _factor(self, c):
        f = 1.0
        if self.in_caution():
            f = 0.6
        if c.pit and c.pit[0] <= self.t < c.pit[1]:
            f = 0.4
        return f

    def step(self):
        dt = self.dt
        for c in self.cars.values():
            if c.idx == ME:
                continue
            c.D += dt / c.lap_s * self._factor(c)
            self._lap_check(c)
        self._player(dt)
        self.t += dt

    def _lap_check(self, c):
        n = int(math.floor(c.D))
        if n > c.completed:
            c.completed = n
            if n >= 1:
                c.last = self.t - c.lap_start
            c.lap_start = self.t

    def _player(self, dt):
        c = self.cars[ME]
        ps = self._pit_state
        if ps is None:
            for p in self.pits:
                if not p["done"] and c.completed + 1 == p["lap"] and c.pct >= 0.9:
                    ps = self._pit_state = {"p": p, "t0": self.t, "pct0": c.pct, "stalled": 0.0, "filled": 0.0,
                                            "phase": "in"}
                    break
        factor = self._factor(c)
        if ps is not None:
            p = ps["p"]
            moving_s = p["lane"] - p["stall"]
            speed = 0.2 / moving_s                       # laps a second on pit road (0.2 lap of pit road)
            if ps["phase"] == "in":
                # drive to the stall: half the lane, stop for the service, drive the other half
                done = (self.t - ps["t0"] - ps["stalled"])
                if done < moving_s / 2:
                    c.D += speed * dt
                else:
                    ps["phase"] = "stall"
            if ps["phase"] == "stall":
                ps["stalled"] += dt
                add_now = min(self.fuel_rate * dt, p["add"] - ps["filled"], self.tank - self.fuel)
                self.fuel += add_now
                ps["filled"] += add_now
                if ps["stalled"] >= p["stall"]:
                    ps["phase"] = "out"
            elif ps["phase"] == "out":
                c.D += speed * dt
                if (self.t - ps["t0"] - ps["stalled"]) >= moving_s:
                    p["done"] = True
                    self._pit_state = None
            self.fuel -= self.burn * 0.5 * (speed * dt if ps["phase"] != "stall" else 0.0)
        else:
            dD = dt / c.lap_s * factor
            c.D += dD
            self.fuel -= self.burn * dD * (0.5 if self.in_caution() else 1.0)
        self._lap_check(c)

    # ------------------------------------------------------------ what iRacing would say
    def _positions(self):
        order = sorted(self.cars.values(), key=lambda c: -c.D)
        pos = {c.idx: k + 1 for k, c in enumerate(order)}
        cls_pos = {}
        for cl in {c.cls for c in self.cars.values()}:
            for k, c in enumerate([c for c in order if c.cls == cl]):
                cls_pos[c.idx] = k + 1
        return order, pos, cls_pos

    def own(self):
        c = self.cars[ME]
        order, pos, cls_pos = self._positions()
        flags = 0x4 | self.flags_extra
        if self.in_caution():
            flags = 0x4000 | 0x8000 | self.flags_extra
        ps = self._pit_state
        on_pit = ps is not None
        in_stall = bool(ps and ps["phase"] == "stall")
        if self.timed_s is not None:
            remain, lrem = max(0.0, self.timed_s - self.t), 32767
        else:
            remain, lrem = 604800.0, max(0, self.laps - c.completed)
        t = self.t
        if self.freeze_from is not None and self.t >= self.freeze_from:
            t = self.freeze_from
        return {"SessionTime": t, "SessionNum": 2, "SessionType": self.session_type, "Lap": c.completed + 1,
                "LapDistPct": c.pct, "LapLastLapTime": c.last, "Speed": 40.0, "Brake": self.brake,
                "Throttle": self.throttle, "Gear": 4, "OnPitRoad": on_pit, "PlayerTrackSurface": 3 if not on_pit else 1,
                "CarLeftRight": self.side, "IsOnTrack": True, "FuelLevel": max(0.0, self.fuel),
                "PlayerCarInPitStall": in_stall, "SessionTimeRemain": remain, "SessionLapsRemainEx": lrem,
                "SessionFlags": flags, "SessionState": self.state, "PlayerCarPosition": pos[ME],
                "PlayerCarClassPosition": cls_pos[ME], "PlayerCarTowTime": 0.0}

    def field(self):
        order, pos, cls_pos = self._positions()
        lead = order[0].D
        n = 64
        pad = lambda f, d=0: [f(self.cars[i]) if i in self.cars else d for i in range(n)]  # noqa: E731
        return {"CarIdxLapCompleted": pad(lambda c: c.completed), "CarIdxLapDistPct": pad(lambda c: c.pct, -1.0),
                "CarIdxLastLapTime": pad(lambda c: c.last, -1.0),
                "CarIdxBestLapTime": pad(lambda c: c.lap_s, -1.0), "CarIdxPosition": pad(lambda c: pos[c.idx]),
                "CarIdxClassPosition": pad(lambda c: cls_pos[c.idx]),
                "CarIdxF2Time": pad(lambda c: (lead - c.D) * self.lap_s if self.f2 else 0.0, 0.0),
                "CarIdxEstTime": pad(lambda c: c.pct * c.lap_s, 0.0),
                "CarIdxOnPitRoad": pad(lambda c: bool((c.pit and c.pit[0] <= self.t < c.pit[1]) or
                                                      (c.idx == ME and self._pit_state is not None))),
                "CarIdxTrackSurface": pad(lambda c: 3, -1)}

    def sections(self):
        drivers = [{"CarIdx": i, "UserName": c.name, "CarNumber": c.num, "CarClassID": c.cls,
                    "CarScreenName": "GT3", "CarIsPaceCar": 0, "IsSpectator": 0} for i, c in self.cars.items()]
        return {"DriverInfo": {"DriverCarIdx": ME, "DriverCarFuelMaxLtr": self.tank, "DriverCarMaxFuelPct": 1.0,
                               "DriverCarEstLapTime": self.lap_s, "Drivers": drivers},
                "WeekendInfo": {"TrackDisplayName": "Test Ring", "TrackName": "testring"}}

    def run(self, seconds=None, field_every=0.25):
        """Yields (own, field or None) every dt; the field arrays about 4 times a second (the same dict until they change)."""
        end = seconds if seconds is not None else 10 ** 9
        f = None
        nxt = 0.0
        while self.t < end and not self.finished():
            self.step()
            if self.t >= nxt:
                f = self.field()
                nxt = self.t + field_every
            yield self.own(), f

    def finished(self):
        c = self.cars[ME]
        if self.timed_s is not None:
            return False
        return c.completed >= self.laps
