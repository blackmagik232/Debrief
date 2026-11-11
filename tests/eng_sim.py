"""A scripted car for the Race Engineer tests (2.31): fake session text and 20 Hz readings. No iRacing, no network.

    sim = Sim(kind="practice")          # also "test", "open_practice", "lone_qualify", "open_qualify", "warmup", "race" ...
    sim.leave_pits()                    # garage -> pit road -> out lap
    sim.run(seconds, lap_s=90.0)        # drive; each call yields nothing, readings go to the callback
"""
from __future__ import annotations

TYPES = {
    "test": ("Offline Testing", "TESTING", "Test", 0, 0, 0),
    "practice": ("Practice", "PRACTICE", "Practice", 1, 0, 0),
    "open_practice": ("Open Practice", "OPEN PRACTICE", "Practice", 1, 0, 0),
    "lone_qualify": ("Lone Qualify", "QUALIFY", "Qualify", 1, 0, 0),
    "open_qualify": ("Open Qualify", "QUALIFY", "Qualify", 1, 0, 0),
    "qualify": ("Qualify", "QUALIFY", "Qualify", 1, 0, 0),
    "warmup": ("Warmup", "WARMUP", "Race", 1, 0, 0),
    "race": ("Race", "RACE", "Race", 1, 0, 0),
    "time_trial": ("Time Trial", "TIME TRIAL", "Time Trial", 1, 0, 0),
    "hosted": ("Practice", "PRACTICE", "Practice", 0, 0, 0),
    "league": ("Race", "RACE", "Race", 0, 0, 1234),
    "weird": ("Lap Dash", "DASH", "Dash", 1, 0, 0),
}


def sections(kind="practice", n_cars=4, sectors=3, spectator=False, replay=False, split=True, classes=1):
    typ, name, event, official, series_zero, league = TYPES[kind]
    drivers = [{"CarIdx": 0, "UserName": "Pace Car", "CarIsPaceCar": 1, "CarNumber": "0", "CarClassID": 0}]
    for i in range(1, n_cars + 1):
        drivers.append({"CarIdx": i, "UserName": f"Driver {i}", "CarNumber": str(10 + i), "UserID": 100 + i,
                        "CarClassID": 11 if (classes == 1 or i % 2) else 22, "CarScreenName": "Mazda MX-5", "CarClassShortName": "MX5",
                        "IsSpectator": 1 if (spectator and i == 1) else 0, "TeamID": 0})
    wi = {"TrackDisplayName": "Test Raceway", "TrackName": "testway", "EventType": event, "Official": official,
          "SeriesID": 0 if (not official) else 77, "LeagueID": league, "TeamRacing": 0, "NumCarClasses": classes,
          "SubSessionID": 5551, "SimMode": "replay" if replay else "full"}
    si = {"Sessions": [{"SessionNum": 0, "SessionType": typ, "SessionName": name, "SessionTime": "1800 sec"}]}
    out = {"WeekendInfo": wi, "DriverInfo": {"DriverCarIdx": 1, "DriverUserID": 101, "Drivers": drivers,
                                             "DriverCarFuelMaxLtr": 50.0, "DriverCarMaxFuelPct": 1.0}, "SessionInfo": si}
    if split:
        out["SplitTimeInfo"] = {"Sectors": [{"SectorNum": i, "SectorStartPct": i / sectors} for i in range(sectors)]}
    return out


class Sim:
    """One car on a track. tick(dt) advances the clock and returns the reading dict (own channels)."""

    def __init__(self, kind="practice", lap_s=90.0, hz=20.0, fuel=40.0, burn=2.0, **kw):
        self.kind = kind
        self.sections = sections(kind, **kw)
        self.dt = 1.0 / hz
        self.t = 100.0
        self.pct = 0.0
        self.lap = 0
        self.lap_s = lap_s
        self.last = -1.0
        self.best = -1.0
        self.fuel = fuel
        self.burn = burn
        self.pit = False
        self.garage = True
        self.surf = -1
        self.on_track = False
        self.state = 4 if kind != "race" else 4
        self.flags = 0x4
        self.remain = 1800.0
        self.sector_scale = None          # {sector index: seconds added in that sector} for the next lap
        self.incidents = 0
        self.extra = {}
        self.air, self.track = 22.0, 35.0
        self.brake = 0.0
        self.slow = {}
        self.lap_t0 = self.t

    def own(self):
        s = {"SessionTime": self.t, "SessionNum": 0, "SessionUniqueID": 9, "Lap": self.lap, "LapDistPct": self.pct,
             "LapLastLapTime": self.last, "LapBestLapTime": self.best, "Speed": 40.0, "Brake": self.brake, "Throttle": 0.5,
             "Gear": 3, "OnPitRoad": self.pit, "PlayerTrackSurface": self.surf, "CarLeftRight": 1,
             "IsOnTrack": self.on_track, "IsInGarage": self.garage, "PlayerCarMyIncidentCount": self.incidents,
             "FuelLevel": self.fuel, "SessionTimeRemain": self.remain, "SessionLapsRemainEx": 32767,
             "SessionFlags": self.flags, "SessionState": self.state, "AirTemp": self.air, "TrackTempCrew": self.track,
             "TrackWetness": 1, "PlayerCarPosition": 3, "PlayerCarClassPosition": 3, "PlayerCarTowTime": 0.0,
             "SteeringWheelAngle": 0.0}
        s.update(self.extra)
        return s

    def tick(self, dt=None):
        dt = self.dt if dt is None else dt
        self.t += dt
        self.remain = max(0.0, self.remain - dt)
        if self.on_track and not self.pit:
            f = dt / self.lap_s
            e = self.slow.get(min(2, int(self.pct * 3)), 0.0)         # a slow sector: it takes e seconds longer
            if e:
                f *= (self.lap_s / 3.0) / (self.lap_s / 3.0 + e)
            self.pct += f
            self.fuel -= self.burn * f
            if self.pct >= 1.0:
                over = (self.pct - 1.0) / f * dt if f else 0.0
                self.pct -= 1.0
                self.lap += 1
                self.last = (self.t - over) - self.lap_t0
                self.lap_t0 = self.t - over
                if self.best < 0 or self.last < self.best:
                    self.best = self.last
        return self.own()

    penalty = 0.0
    slow: dict = {}
    lap_t0 = 100.0

    def leave_pits(self):
        self.garage, self.pit, self.surf, self.on_track = False, True, 1, True
        self.lap_t0 = self.t
        yield_ = [self.own()]
        self.pit, self.surf = False, 3
        return yield_

    def in_garage(self):
        self.garage, self.pit, self.surf, self.on_track = True, False, -1, False

    def to_pits(self):
        self.pit, self.surf, self.garage = True, 1, False
