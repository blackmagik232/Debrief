"""A scripted multiclass TEAM race for the 2.30 Race Room tests: three drivers share one car, the car stops for fuel and a
driver swap, there is a caution, a long stop and a driver who is late, and the same race can be read from three PCs:

    pc="driver"  this PC's user is the driver of the stint (all the own-car channels, fuel included)
    pc="crew"    this PC's user is in the session but not in the car: iRacing sends every car's CarIdx data and the
                 session text, but the own-car channels (FuelLevel, PitSv...) are absent
    pc="driver" with pc_user set to a driver who is not in the car at the moment is the same as crew until his turn

    sim = EnduSim(timed_s=5400, pc_user=1)           # this PC belongs to driver 1 (Ben); he drives stint 2
    sim.stop(lap=29, add=58, stall=35, lane=60, to=1)    # lap 29: fuel and swap to driver 1
    for own, field in sim.run(): ...
    sim.sections(), sim.info_version                 # the session text (changes when the driver changes)

No iRacing, no files, no network. Same units as crew_sim: litres, seconds, laps.
"""
from __future__ import annotations

import crew_sim as S

DRIVERS = ["Alex Able", "Ben Baker", "Cal Cole"]
TEAM_ID = 77
TEAM_NAME = "Test Team"
UID0 = 1000


class EnduSim(S.RaceSim):
    def __init__(self, pc_user=0, listing="current", lap_s=60.0, tank=60.0, burn=2.0, timed_s=5400.0, cars=12,
                 drivers=DRIVERS, pace=(0.0, 0.4, -0.2), burns=(0.0, 0.0, 0.0), dt=0.25, multiclass=True, **kw):
        super().__init__(laps=10 ** 6, tank=tank, burn=burn, lap_s=lap_s, dt=dt, cars=cars, timed_s=timed_s,
                         multiclass=multiclass, **kw)
        self.drivers = list(drivers)
        self.pace = list(pace)
        self.burns = list(burns)
        self.base_lap, self.base_burn = lap_s, burn
        self.pc_user = pc_user              # which driver this PC's user is (None: never in the car)
        self.listing = listing              # "current": iRacing lists the driver in the car; "all": all of them, same CarIdx
        self.driver_i = 0
        self.swap_to: dict = {}             # stop lap -> driver index who takes over at the end of that stop
        self.info_version = 1
        self.incidents = 0
        self.stall_cars: dict = {}
        self.fuel_channel = True            # False: even the driver's PC has no FuelLevel (a test of the unknown case)
        for c in self.cars.values():
            if c.cls == S.CLASS_B and c.idx != S.ME:
                c.lap_s *= 0.92
        self._apply_driver()

    # ------------------------------------------------------------ script
    def stop(self, lap, add, stall=35.0, lane=60.0, to=None):
        """The car stops at the end of `lap`, takes `add` litres, sits `stall` s, `lane` s on pit road; `to`: the driver who
        takes over (None: the same driver continues)."""
        self.pit_at(lap, add, stall=stall, lane_s=lane)
        if to is not None:
            self.swap_to[lap] = to

    def car_stop(self, idx, t0, t1):
        self.car_pit(idx, t0, t1)
        self.stall_cars[idx] = (t0 + 0.3 * (t1 - t0), t0 + 0.7 * (t1 - t0))

    def driving(self):
        return self.pc_user is not None and self.driver_i == self.pc_user

    def _apply_driver(self):
        i = self.driver_i
        self.cars[S.ME].lap_s = self.base_lap + self.pace[i]
        self.burn = self.base_burn + self.burns[i]

    def finished(self):
        """A timed race is over a lap and a bit after the clock reaches zero (the car takes the flag)."""
        return self.timed_s is not None and self.t >= self.timed_s + 1.2 * self.cars[S.ME].lap_s

    # ------------------------------------------------------------ the world
    def _player(self, dt):
        before = sum(1 for p in self.pits if p["done"])
        super()._player(dt)
        if sum(1 for p in self.pits if p["done"]) > before:
            p = next(p for p in reversed(self.pits) if p["done"])
            to = self.swap_to.get(p["lap"])
            if to is not None and to != self.driver_i:
                self.driver_i = to
                self.info_version += 1
                self._apply_driver()
                self.incidents = 0

    # ------------------------------------------------------------ what iRacing says
    def own(self):
        o = super().own()
        if self.driving():
            if not self.fuel_channel:
                o["FuelLevel"] = None
            o.update({"DCDriversSoFar": self.driver_i + 1, "DCLapStatus": 0, "PlayerCarTeamIncidentCount": self.incidents,
                      "PitSvFlags": 0x10 | (0x0F if self._pit_state and self._pit_state["p"]["add"] > 0 else 0),
                      "PlayerCarIdx": S.ME})
            return o
        # not in the car: the session clock and flags are there, the own-car channels are not
        for k in ("FuelLevel", "PlayerCarInPitStall", "PlayerCarTowTime", "CarLeftRight", "PitSvFlags"):
            o[k] = None
        o.update({"IsOnTrack": False, "OnPitRoad": False, "Lap": 0, "LapDistPct": -1.0, "LapLastLapTime": -1.0,
                  "PlayerCarPosition": 0, "PlayerCarClassPosition": 0, "Speed": 0.0, "Brake": 0.0, "Throttle": 0.0,
                  "PlayerCarIdx": S.ME})
        return o

    def field(self):
        f = super().field()
        n = len(f["CarIdxOnPitRoad"])
        surf = [3] * n
        ps = self._pit_state
        if ps is not None and ps["phase"] == "stall":
            surf[S.ME] = 1
        elif ps is not None:
            surf[S.ME] = 2
        for i, (a, b) in self.stall_cars.items():
            if a <= self.t < b:
                surf[i] = 1
        f["CarIdxTrackSurface"] = surf
        f["CarIdxFastRepairsUsed"] = [0] * n
        f["CarIdxTireCompound"] = [0] * n
        return f

    def sections(self):
        sec = super().sections()
        di = sec["DriverInfo"]
        drivers = []
        for d in di["Drivers"]:
            if d["CarIdx"] == S.ME:
                idxs = [self.driver_i] if self.listing == "current" else range(len(self.drivers))
                for k in idxs:
                    drivers.append(dict(d, UserName=self.drivers[k], UserID=UID0 + k, TeamID=TEAM_ID, TeamName=TEAM_NAME,
                                        IsSpectator=0))
            else:
                drivers.append(dict(d, UserID=5000 + d["CarIdx"], TeamID=0, TeamName=""))
        di["Drivers"] = drivers
        di["DriverCarIdx"] = S.ME
        # a PC whose user is never in the car is a crew member the session text does not list (not one of the three drivers)
        di["DriverUserID"] = UID0 + self.pc_user if self.pc_user is not None else UID0 + 777
        sec["WeekendInfo"].update({"TeamRacing": 1, "SubSessionID": 4242})
        sec["SessionInfo"] = {"Sessions": [{"SessionNum": 2, "SessionType": "Race",
                                            "SessionTime": f"{self.timed_s:.4f} sec" if self.timed_s else "unlimited"}]}
        return sec


def plan_inputs(minutes=90, tank=60.0, lap_t=60.0, burn=2.0, names=DRIVERS, tz=(0, 0, 0), paces=(0.0, 0.4, -0.2)):
    """The saved stint plan that goes with EnduSim's defaults: 90 minutes, three drivers, 29 laps a tank (stops at the end of
    laps 29, 58 and 87 with a swap each time), pit lane 25 s, 5 L/s, so a swap stop is 25 + 30 = 55 s."""
    return {"minutes": minutes, "tank_l": tank, "reserve_laps": 1, "pit_lane_s": 25, "fuel_rate_lps": 5, "tyre_s": 0,
            "tyre_stints": 0, "fair_share": True, "stints_in_row": 1,
            "drivers": [{"name": n, "lap_t": lap_t + paces[i], "burn_l": burn, "tz": tz[i]} for i, n in enumerate(names)]}


STD_STOPS = ((29, 58, 1), (58, 58, 2), (87, 50, 0))       # (lap, litres, driver who takes over)


def standard_sim(pc_user=0, **kw):
    """Three drivers, three swaps (Alex -> Ben -> Cal -> Alex), 90 minutes, 60 s laps: the stops of plan_inputs()."""
    sim = EnduSim(pc_user=pc_user, **kw)
    for lap, add, to in STD_STOPS:
        sim.stop(lap, add, to=to)
    return sim


def run(sim, plan=True, iam=None, units="kmh", log=None, seconds=None, wall0=1_800_000_000.0, on_step=None, tz=(0, 0, 0)):
    """Feeds the sim through Race Room and the crew chief the way the coach does (pre, crew chief, post).
    Returns (room, chief, alerts, events)."""
    from debrief import crewchief as cc
    from debrief import raceroom as RR
    saved = {"inputs": plan_inputs(tz=tz), "title": "Test plan", "saved": "now"} if plan is True else plan
    who = iam if iam is not None else (DRIVERS[sim.pc_user] if sim.pc_user is not None else "")
    room = RR.RaceRoom(units=units, plan_for=(lambda track: saved) if saved else None, iam=lambda: who,
                       wall=lambda: wall0 + sim.t, log=log)
    chief = cc.CrewChief(units=units, clock=lambda: sim.t)
    ver, alerts, events = None, [], []
    for own, f in sim.run(seconds):
        secs = None
        if sim.info_version != ver:
            ver = sim.info_version
            secs = sim.sections()
            chief.set_info(secs, ver)
        o2 = room.pre(own, f, secs, ver, sim.t)
        chief.feed(o2, f, now=sim.t)
        room.post(chief, sim.t, own)
        alerts += room.take_alerts()
        events += room.take_events()
        if on_step:
            on_step(room, chief, own, f)
    return room, chief, alerts, events
