"""The Race Room's own live link to iRacing (2.36 "Race Room"): the page works whenever Debrief is open, with or without
the voice coach.

Before 2.36 the Race Room only existed inside the running voice coach, and only showed once a race was green. Now the
window keeps one small reader of its own (livedata through livecoach.IrsdkSource, about 5 readings a second, the field
arrays 4 times a second) and feeds it to a silent crew chief, the Race Room and the pit board (roomboard). Nothing here
speaks: the coach, when it runs, still makes the spoken calls itself.

    rl = RoomLive(plan_for=..., iam=..., log=..., units=lambda: "mph")
    rl.start(); rl.state(); rl.peek(); rl.stop()

Tests drive tick() themselves with a stand-in source (read() -> own dict with "_field", info_sections() -> (v, sections)).
Nothing here talks to the network.
"""
from __future__ import annotations

import threading
import time

from . import crewchief, perfmeter, raceroom, roomboard

RATE_HZ = 5.0
STATE_EVERY_S = 0.5
IDLE_SLEEP_S = 1.0
LOST_AFTER_S = 3.0


def _session_type(sections, num) -> str:
    try:
        for x in (sections.get("SessionInfo") or {}).get("Sessions") or []:
            if x.get("SessionNum") == num:
                return str(x.get("SessionType") or x.get("SessionName") or "")
    except (AttributeError, TypeError):
        pass
    return ""


class RoomLive:
    def __init__(self, source=None, plan_for=None, iam=None, log=None, units=None, clock=time.monotonic, wall=time.time,
                 say=None, chief_plan_for=None, log_factory=None):
        self._source = source
        self.plan_for = plan_for                  # track -> endurance.load_inputs' answer (the Race Room's stint plan)
        self.chief_plan_for = chief_plan_for      # track -> crewchief.load_plan's answer (the crew chief's stops)
        self.units = units or (lambda: "mph")
        self.clock, self.wall = clock, wall
        self.say = say or (lambda m: None)
        self.chief = crewchief.CrewChief(units=self._units(), clock=clock)
        # the team-race log: one file per race session (log_factory() -> an endurancelog.RaceLog), or log(row) for tests
        self.log_factory = log_factory
        self._log_fixed = log
        self._log = None
        self._log_key = None
        self._kind = "none"
        self.room = raceroom.RaceRoom(units=self._units(), plan_for=plan_for, iam=iam,
                                      log=self._log_row if (log or log_factory) else None, wall=wall)
        self.book = roomboard.LapBook()
        self.perf = perfmeter.PerfMeter("raceroom page", on_slow=lambda m: self.say("roomlive: " + m))
        self._info_v = None
        self._sections: dict = {}
        self._plan_key = None
        self._latest = None
        self._last_read = None
        self._state = roomboard.waiting()
        self._state_t = -1e9
        self._peek = {"connected": False, "session": None}
        self._thread = None
        self._stop = threading.Event()
        self.errors = 0
        self.last_error = ""

    def _units(self):
        try:
            return self.units() if callable(self.units) else (self.units or "mph")
        except Exception:  # noqa: BLE001
            return "mph"

    @property
    def source(self):
        if self._source is None:
            from . import livecoach
            self._source = livecoach.IrsdkSource(rate_hz=RATE_HZ, retry_s=3.0, crew=True)
        return self._source

    def _log_row(self, row):
        if self._kind != "race":
            return                              # the team-race log is for races only
        if self._log_fixed is not None:
            self._log_fixed(row)
            return
        if self._log is None and self.log_factory is not None:
            try:
                self._log = self.log_factory()
            except Exception:  # noqa: BLE001
                self._log, self.log_factory = None, None
        if self._log is not None:
            self._log.add(row)

    def _close_log(self):
        lg, self._log = self._log, None
        if lg is not None:
            try:
                lg.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ the thread
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="roomlive")
        self._thread.start()
        return True

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout=3.0):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._close_log()
        try:
            if self._source is not None:
                self._source.close()
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            got = False
            try:
                got = self.tick()
            except Exception as e:  # noqa: BLE001  (the page must never take the window down)
                self.errors += 1
                self.last_error = f"{e.__class__.__name__}: {str(e)[:160]}"
                if self.errors <= 5 or self.errors % 100 == 0:
                    self.say("roomlive: ERROR " + self.last_error)
            wait = (1.0 / RATE_HZ if got else IDLE_SLEEP_S) - (time.monotonic() - t0)
            if wait > 0:
                self._stop.wait(wait)

    # ------------------------------------------------------------ one reading
    def tick(self) -> bool:
        """Read iRacing once and work everything out. False when iRacing gave nothing (not running)."""
        now = self.clock()
        src = self.source
        s = src.read()
        if not s:
            if self._last_read is None or now - self._last_read > LOST_AFTER_S:
                state = getattr(src, "state", "not_running")
                detail = ("iRacing's live link isn't available on this PC." if state == "not_installed" else
                          "Open iRacing and join a session: this page fills in by itself.")
                self._state = roomboard.waiting("Waiting for iRacing.", detail)
                self._peek = {"connected": False, "session": None}
            return False
        self._last_read = now
        w0, c0 = time.perf_counter(), time.thread_time()
        units = self._units()
        self.chief.units = units
        self.room.units = units
        fn = getattr(src, "info_sections", None)
        ver, secs = fn() if callable(fn) else (None, None)
        new_info = bool(secs) and ver != self._info_v
        if new_info:
            self._info_v = ver
            self._sections = secs
            self.chief.set_info(secs, ver)
        key = self.chief.track_name
        if key and key != self._plan_key and self.chief_plan_for is not None:
            self._plan_key = key
            try:
                self.chief.set_plan(self.chief_plan_for(key))
            except Exception:  # noqa: BLE001
                self.chief.set_plan(None)
        field = s.get("_field")
        num = s.get("SessionNum")
        typ = str(s.get("SessionType") or "") or _session_type(self._sections, num)
        s["SessionType"] = typ
        uid = s.get("SessionUniqueID")
        skey = f"{uid if uid is not None else (self.room.info.get('sub') or '')}:{num}"
        if skey != self._log_key:
            self._log_key = skey
            self._close_log()                    # a new session: the next race gets a log file of its own
        self._kind = roomboard.session_kind(typ)
        room = self.room
        own = room.pre(s, field, secs if new_info else None, ver, now)
        room.bind(self.chief)
        self.chief.feed(own, field, now=now)
        room.post(self.chief, now, s)
        room.take_alerts()
        room.take_events()
        me = room.team_idx if room.team_idx is not None else self.chief.me
        drv = room.cur_driver or (self.chief.drivers.get(me) or {}).get("name", "")
        self.book.feed(s, field, me, drv)
        sess = {"key": skey, "kind": roomboard.session_kind(typ), "label": roomboard.session_label(typ)}
        self._peek = {"connected": True, "session": sess}
        if now - self._state_t >= STATE_EVERY_S:
            self._state_t = now
            st = roomboard.build(s=s, field=field, room=room, chief=self.chief, book=self.book, typ=typ, session_key=skey,
                                 track=room.info.get("track") or self.chief.track_name, car=room.info.get("car") or self.chief.car_name,
                                 units=units)
            if st["team"] or (st["session"]["kind"] == "race" and room.plan is not None):
                st["room"] = room.state()                     # the team panels (stint, swap, rest of the race)
            st["role"] = dict(room.role)
            self._state = st
        self.perf._done(w0, c0)
        return True

    # ------------------------------------------------------------ for the window
    def state(self) -> dict:
        st = dict(self._state)
        st["perf"] = self.perf.summary()
        return st

    def peek(self) -> dict:
        return dict(self._peek)
