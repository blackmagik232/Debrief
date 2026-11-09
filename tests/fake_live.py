"""A stand-in for iRacing's live data (2.27): a scripted practice or short race, as both a reader (what the recorder
asks for) and a pyirsdk-shaped module (what livedata.LiveReader asks for).

    sim = Sim(kind="race")
    rd = FakeReader(sim)          # read_own / read_field / info_sections, driven by sim.step(dt)
    sim.step(0.05)

One time axis: sim.t is the session clock in seconds and also the recorder's clock (sim.now)."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

LAP_S = 60.0
ME = 2
NAMES = ["Pace Car", "A Able", "Me Driver", "C Choate", "D Dunn", "E Eyre"]
NUMS = ["0", "11", "22", "33", "44", "55"]


class Sim:
    def __init__(self, kind="practice", sub=1234567, aids=True, laps=6, pit_lap=None, track="Circuit de Spa-Francorchamps",
                 car="BMW M4 GT3", cars=6, first_sn=None, config="Grand Prix Pits", base_utc=None):
        self.kind, self.sub, self.aids, self.nlaps = kind, sub, aids, laps
        self.track_name, self.car_name, self.config = track, car, config
        self.base_utc = base_utc or datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
        self.cars = cars
        self.t = 0.0
        self.now = 1000.0                     # monotonic clock for the recorder
        self.sn = (2 if kind == "race" else 0) if first_sn is None else first_sn
        self.connected = True
        self.pit_lap = pit_lap if pit_lap is not None else (3 if kind == "race" else None)
        self.fuel = 60.0
        self.inc = 0
        self.bb = 54.0
        self.disk = True
        self.events = []                      # (time, fn)
        self.info_version = 1
        self.extra_drivers = []
        self.wall = datetime(2026, 11, 8, 19, 30)
        self.lap_times = {}
        self.disk_known = True
        self.missing = set() if aids else {"dcBrakeBias", "dcABS", "dcTractionControl"}

    def utc(self):
        return (self.base_utc + timedelta(seconds=self.t)).isoformat(timespec="seconds")

    # ----------------------------------------------------------- time
    def step(self, dt=0.05):
        for ev in [e for e in self.events if e[0] <= self.t + dt and not e[2]]:
            ev[2].append(1)
            ev[1](self)
        self.t += dt
        self.now += dt

    def at(self, t, fn):
        self.events.append([t, fn, []])

    # ----------------------------------------------------------- the player's car
    def _lapinfo(self, idx, lap_s):
        """(laps completed, fraction round the lap) for car idx at the session clock."""
        run = max(0.0, self.t - 2.0 * (idx))      # staggered starts
        done = int(run // lap_s)
        return done, (run % lap_s) / lap_s

    def my_lap_s(self):
        return LAP_S

    def _my_pit(self):
        """On pit road during lap pit_lap (index of the lap being driven, 1-based) between 20 and 45 s into it."""
        if self.pit_lap is None:
            return False, False
        done, frac = self._lapinfo(ME, LAP_S)
        lap = done + 1
        s = frac * LAP_S
        if lap == self.pit_lap and 20 <= s < 45:
            return True, 25 <= s < 40
        return False, False

    def own(self):
        done, frac = self._lapinfo(ME, LAP_S)
        pit, stall = self._my_pit()
        # fuel: 0.05 L/s burn, 30 L added in the stall (continuous)
        burn = 0.05 * self.t
        added = 0.0
        if self.pit_lap is not None:
            start = (self.pit_lap - 1) * LAP_S + 25.0
            added = 30.0 * min(1.0, max(0.0, (self.t - start) / 15.0))
        fuel = self.fuel - burn + added
        d = {"SessionTime": self.t, "SessionNum": self.sn, "Lap": done + 1, "LapDistPct": frac,
             "LapLastLapTime": LAP_S if done >= 1 else -1.0, "Speed": 55.0, "Brake": 0.0, "Throttle": 1.0, "Gear": 4,
             "OnPitRoad": pit, "PlayerTrackSurface": 1 if stall else (2 if pit else 3), "CarLeftRight": 1, "IsOnTrack": True,
             "PlayerCarMyIncidentCount": self.inc, "SteeringWheelAngle": 0.0,
             "FuelLevel": fuel, "FuelLevelPct": fuel / 100.0, "FuelUsePerHour": 180.0,
             "dcBrakeBias": self.bb, "dcABS": 3, "dcTractionControl": 2,
             "PitSvFlags": 0, "PitSvFuel": 0.0, "PlayerCarPitSvStatus": 0, "PitstopActive": stall, "PlayerCarInPitStall": stall,
             "PitRepairLeft": 0.0, "PitOptRepairLeft": 0.0, "FastRepairAvailable": 1, "FastRepairUsed": 0,
             "TireSetsUsed": 1 + (1 if self.pit_lap is not None and self.t > (self.pit_lap - 1) * LAP_S + 40 else 0),
             "TireSetsAvailable": 4, "LFwearM": 0.99, "RFwearM": 0.99, "LRwearM": 0.99, "RRwearM": 0.99,
             "PlayerCarTeamIncidentCount": self.inc, "PlayerCarDriverIncidentCount": self.inc, "DCDriversSoFar": 1,
             "DCLapStatus": 0, "SessionTimeRemain": 3600.0 - self.t, "SessionLapsRemainEx": 32767, "SessionFlags": 0x4,
             "SessionState": 4, "TrackTempCrew": 24.5, "AirTemp": 18.0, "TrackWetness": 1,
             "IsDiskLoggingEnabled": self.disk if self.disk_known else None,
             "PlayerCarPosition": 3, "PlayerCarClassPosition": 3}
        for k in self.missing:
            d.pop(k, None)
        if not self.disk_known:
            d.pop("IsDiskLoggingEnabled")
        return d

    # ----------------------------------------------------------- the field
    def field(self):
        n = 64
        lap, pct, last, best, pos, cpos, f2, est, pit, surf, tc, frp = ([0] * n for _ in range(12))
        for i in range(n):
            if i >= self.cars:
                lap[i], pct[i], pos[i], cpos[i], surf[i] = -1, -1.0, 0, 0, -1
                last[i] = best[i] = -1.0
                continue
            ls = LAP_S + (i - ME) * 0.5 if i != 0 else LAP_S
            done, frac = self._lapinfo(i, ls)
            if i == ME:
                done, frac = self._lapinfo(ME, LAP_S)
            lap[i], pct[i] = done, frac
            last[i] = ls if done else -1.0
            best[i] = ls if done else -1.0
            pos[i] = cpos[i] = (i if i else 0)
            f2[i] = i * 1.5
            est[i] = frac * ls
            tc[i] = 0
            surf[i] = 3
            onpit = False
            if i == ME:
                onpit, st = self._my_pit()
                surf[i] = 1 if st else (2 if onpit else 3)
            elif i == 4 and self.kind == "race":                   # car 4 stops on its lap 3 for 20 s, 8 s of it in the stall
                lp = done + 1
                s = frac * ls
                if lp == 3 and 10 <= s < 30:
                    onpit = True
                    surf[i] = 1 if 14 <= s < 22 else 2
            pit[i] = onpit
        return {"CarIdxLapCompleted": lap, "CarIdxLapDistPct": pct, "CarIdxLastLapTime": last, "CarIdxBestLapTime": best,
                "CarIdxPosition": pos, "CarIdxClassPosition": cpos, "CarIdxF2Time": f2, "CarIdxEstTime": est,
                "CarIdxOnPitRoad": pit, "CarIdxTrackSurface": surf, "CarIdxTireCompound": tc, "CarIdxFastRepairsUsed": frp}

    # ----------------------------------------------------------- session info
    def sections(self):
        drivers = []
        for i in range(self.cars):
            drivers.append({"CarIdx": i, "UserName": NAMES[i % len(NAMES)], "CarNumber": NUMS[i % len(NUMS)],
                            "TeamName": "", "CarClassShortName": "GT3", "CarScreenName": self.car_name,
                            "CarPath": "bmwm4gt3", "CarID": 132, "IsSpectator": 0, "CarIsPaceCar": 1 if i == 0 else 0})
        drivers += copy.deepcopy(self.extra_drivers)
        sessions = [{"SessionNum": 0, "SessionType": "Practice", "SessionName": "PRACTICE"},
                    {"SessionNum": 1, "SessionType": "Lone Qualify", "SessionName": "QUALIFY"},
                    {"SessionNum": 2, "SessionType": "Race", "SessionName": "RACE",
                     "ResultsPositions": [{"Position": 3, "ClassPosition": 2, "CarIdx": ME, "LapsComplete": self.nlaps, "Incidents": self.inc}]}]
        return {"WeekendInfo": {"TrackName": "spa up", "TrackDisplayName": self.track_name, "TrackConfigName": self.config,
                                "TrackID": 163, "TrackLength": "7.00 km", "SubSessionID": self.sub, "SessionID": self.sub + 1},
                "DriverInfo": {"DriverCarIdx": ME, "DriverCarFuelMaxLtr": 100.0, "DriverCarMaxFuelPct": 1.0, "Drivers": drivers},
                "SessionInfo": {"Sessions": sessions}}


class FakeReader:
    """What Recorder needs from livedata.LiveReader, driven by a Sim."""
    interval = 0.05

    def __init__(self, sim):
        self.sim = sim
        self.closed = False
        self.sections = sim.sections()
        self.version = 1
        self.reads = 0

    def refresh_info(self):
        self.sections = self.sim.sections()
        self.version += 1

    def read_own(self, keep_none=True):
        if not self.sim.connected:
            return None
        self.reads += 1
        return self.sim.own()

    def read_field(self):
        return self.sim.field() if self.sim.connected else None

    def info_sections(self):
        return (self.version, dict(self.sections)) if self.sim.connected else (self.version, {})

    def close(self):
        self.closed = True


class FakeIR:
    """pyirsdk's IRSDK as livedata.LiveReader uses it, replaying a Sim. Variables the sim lacks read as None, exactly
    like a name pyirsdk doesn't know."""
    instances = []

    def __init__(self):
        self.sim = None
        self.is_initialized = True
        self.freezes = 0
        self.gets = {}
        self._own, self._field = {}, {}
        type(self).instances.append(self)

    # set by the test before the first read: FakeIR.sim_for_next
    @property
    def is_connected(self):
        return bool(self.sim and self.sim.connected)

    def startup(self):
        self.sim = type(self).sim_for_next
        return self.sim.connected

    def shutdown(self):
        pass

    def freeze_var_buffer_latest(self):
        self.freezes += 1
        self._own = self.sim.own()
        self._field = self.sim.field()

    def unfreeze_var_buffer_latest(self):
        pass

    @property
    def session_info_update(self):
        return self.sim.info_version

    @property
    def var_headers_names(self):
        return list(self.sim.own()) + list(self.sim.field())

    def __getitem__(self, k):
        self.gets[k] = self.gets.get(k, 0) + 1
        sec = self.sim.sections()
        if k in sec:
            return sec[k]
        if k in self._field:
            return self._field[k]
        return self._own.get(k)
