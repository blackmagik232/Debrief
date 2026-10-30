"""Garage 61 API client (official v1 API, personal access token).

Stays inside Garage 61's usage rules:
  * non-commercial personal use
  * light traffic: every request is paced (min gap between calls),
    nothing is ever downloaded twice (disk cache), and the client
    backs off on 429 / 5xx responses.
The token is only ever sent in the Authorization header. It is never logged,
written to the cache, or put into any output file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import fsutil

# requests is imported when the first client is made (not at import time), so modules that only read the
# saved cache (peek_saved, the window's pickers) start faster.

BASE = "https://garage61.net/api/v1/"


class Garage61Error(RuntimeError):
    """A request to Garage 61 failed. status: the HTTP status (None when Garage 61 couldn't be reached)."""

    def __init__(self, msg="", status=None):
        super().__init__(msg)
        self.status = status

    @property
    def fatal(self) -> bool:
        """True when retrying other requests won't help (the token was rejected)."""
        return self.status == 401


# What the user sees when a request fails. The full detail (path, query, response body) only goes to the log.
MSG_OFFLINE = "Debrief can't reach Garage 61. Check your internet connection, then try again."
MSG_TOKEN = "Garage 61 didn't accept that token. Copy it again from garage61.net and paste it in Settings."
MSG_SERVER = "Garage 61 had a problem (error {code}). Try again in a few minutes."
MSG_BUSY = "Garage 61 is busy right now. Try again in a few minutes."
MSG_FORBIDDEN = ("Garage 61 says this isn't available to your account (error 403). Some data needs a Pro plan "
                 "or a different privacy setting.")
MSG_OTHER = "Garage 61 couldn't answer that request (error {code}). Try again later."
MSG_NOT_DATA = "Garage 61 sent an answer Debrief couldn't read. Try again later."
MSG_NO_TOKEN = "No Garage 61 token saved yet. Open Settings (or the welcome screen) and paste your token."


class Interrupted(Exception):
    """should_stop() turned True while the client was waiting (a backoff or quota wait). Nothing was lost."""


def _atomic_write(p: Path, text: str) -> None:
    """Write a file so it's either the old or the new content, never half of either (temp file, flushed
    to disk, then swapped in)."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=p.suffix, dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Request pacing is shared by every Garage61 client in this process (the sync, a review and a Prep page can
# each make their own client at the same time); one lock and one "last request" time keep them together
# under the ~1 request a second Garage 61 asks for, instead of each pacing only itself.
_PACE_LOCK = threading.Lock()
_PACE = {"last": 0.0}


class Garage61:
    # first wait (seconds) before retrying after a network error, 429 or 5xx; doubles each retry.
    # Tests set it to 0 (Garage61(..., backoff_base=0)) so retries don't sleep.
    BACKOFF_BASE = 5

    def __init__(self, token: str, cache_dir: Path, min_gap_s: float = 1.0,
                 base_url: str = BASE, log=print, stop_event: "threading.Event | None" = None,
                 backoff_base: float | None = None):
        token = (token or "").strip()
        if not token or token.startswith("PASTE"):
            raise Garage61Error(MSG_NO_TOKEN)
        u = urlparse(base_url)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
            raise Garage61Error("The Garage 61 address in your settings must start with https:// so your token "
                                "isn't sent in clear text.")
        self.base = base_url.rstrip("/") + "/"
        if backoff_base is not None:
            self.BACKOFF_BASE = backoff_base
        import requests
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "User-Agent": "Debrief/1.0 (personal, non-commercial)"})
        # cache is namespaced per API address + token fingerprint (never the token itself)
        ns = hashlib.sha256((self.base + "|" + token).encode()).hexdigest()[:10]
        self.cache = Path(cache_dir) / ns
        self.cache.mkdir(parents=True, exist_ok=True)
        self.min_gap = min_gap_s
        self.base_gap = min_gap_s   # the gap widens after a 429 and eases back after a run of successes
        self._ok_run = 0
        self._last = 0.0
        self.requests_made = 0
        self.log = log
        self.should_stop = None   # optional callable: long waits end early (raising Interrupted) when it's True
        self.stop_event = stop_event   # optional threading.Event: the same, for callers that hold an Event

    def _sleep(self, secs: float) -> None:
        """time.sleep that ends early, raising Interrupted, when should_stop() turns True (so a sync can
        pause in under a second even during a 2-minute backoff)."""
        stop, ev = self.should_stop, self.stop_event
        if stop is None and ev is None:
            time.sleep(secs)
            return
        end = time.monotonic() + max(0.0, secs)
        while True:
            if (stop is not None and stop()) or (ev is not None and ev.is_set()):
                raise Interrupted()
            left = end - time.monotonic()
            if left <= 0:
                return
            if ev is not None and stop is None:
                ev.wait(left)            # wakes the moment the event is set
            else:
                time.sleep(min(0.2, left))

    # ---------- low level ----------
    def _wait(self):
        # held across the short pacing sleep, so two clients can't both decide the gap has passed
        with _PACE_LOCK:
            last = max(self._last, _PACE["last"])
            gap = time.monotonic() - last
            if gap < self.min_gap:
                self._sleep(self.min_gap - gap)
            self._last = _PACE["last"] = time.monotonic()

    MAX_GAP = 6.0

    def _slow_down(self):
        """Garage 61 said slow down: space later requests further apart for the rest of this run."""
        new = min(self.MAX_GAP, max(self.min_gap * 1.6, self.base_gap + 0.5))
        if new > self.min_gap:
            self.log(f"  Pacing Garage 61 requests {new:.1f}s apart to stay under its limit")
        self.min_gap = new
        self._ok_run = 0

    def _went_ok(self, r):
        """After 40 requests in a row without a 429, ease the gap back toward normal. If Garage 61 says
        the quota is almost used up, wait for it to reset instead of running into a 429."""
        self._ok_run += 1
        if self.min_gap > self.base_gap and self._ok_run >= 40:
            self.min_gap = max(self.base_gap, self.min_gap * 0.8)
            self._ok_run = 0
        try:
            left = int(r.headers.get("X-RateLimit-Remaining", ""))
            reset = float(r.headers.get("X-RateLimit-Reset", ""))
        except (TypeError, ValueError):
            return
        if left <= 1:
            # Reset is either seconds-to-wait or an epoch time; never wait more than 2 minutes
            secs = reset - time.time() if reset > 1e9 else reset
            secs = max(0.0, min(120.0, secs))
            if secs > 0:
                self.log(f"  Garage 61 request quota nearly used; waiting {secs:.0f}s for it to reset")
                self._sleep(secs)

    @staticmethod
    def _retry_after(r, default):
        v = r.headers.get("Retry-After")
        secs = default
        if v:
            try:
                secs = int(v)
            except ValueError:
                try:
                    secs = (parsedate_to_datetime(v).timestamp() - time.time())
                except (TypeError, ValueError):
                    secs = default
        return max(1 if (v or default >= 1) else 0, min(120, int(secs)))

    def _get(self, path: str, params: dict | None = None, raw: bool = False, forbidden_ok: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for k, v in list(params.items()):
            if isinstance(v, (list, tuple)):
                params[k] = ",".join(map(str, v))
            elif isinstance(v, bool):
                params[k] = "true" if v else "false"
        url = self.base + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        where = "/" + path.lstrip("/") + (("?" + urlencode(params)) if params else "")   # for the log only
        import requests
        delay = self.BACKOFF_BASE
        r = None
        for attempt in range(5):
            last = attempt == 4
            self._wait()
            self.requests_made += 1
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException as e:
                if last:
                    self.log(f"  Garage 61 unreachable for {where}: {type(e).__name__}: {e}")
                    raise Garage61Error(MSG_OFFLINE)
                self._sleep(delay); delay *= 2; continue
            if r.status_code == 401:
                self.log(f"  Garage 61 rejected the token (401) for {where}")
                raise Garage61Error(MSG_TOKEN, status=401)
            if r.status_code == 403:
                if forbidden_ok:
                    return None
                self.log(f"  Garage 61 returned 403 for {where}")
                raise Garage61Error(MSG_FORBIDDEN, status=403)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if r.status_code == 429:
                    self._slow_down()
                if last:
                    break
                wait = self._retry_after(r, delay)
                self.log(f"  Garage 61 asked us to slow down ({r.status_code}); waiting {wait}s")
                self._sleep(wait); delay *= 2; continue
            if not (200 <= r.status_code < 300):
                detail = (r.text or "").strip().replace("\n", " ")[:300]
                self.log(f"  Garage 61 returned {r.status_code} for {where}: {detail}")
                raise Garage61Error(MSG_OTHER.format(code=r.status_code), status=r.status_code)
            self._went_ok(r)
            if raw:
                return r.content.decode("utf-8-sig", errors="replace")
            try:
                return r.json()
            except ValueError:
                self.log(f"  Garage 61 sent something that isn't JSON for {where}")
                raise Garage61Error(MSG_NOT_DATA, status=r.status_code)
        code = r.status_code if r is not None else None
        self.log(f"  Garage 61 kept refusing {where} (last status {code})")
        if code is not None and code >= 500:
            raise Garage61Error(MSG_SERVER.format(code=code), status=code)
        raise Garage61Error(MSG_BUSY, status=code)

    def _cached(self, key: str, fn, max_age_s: float | None = None, suffix=".json"):
        p = self.cache / (hashlib.sha1(key.encode()).hexdigest()[:20] + suffix)
        miss = self.cache / (p.stem + ".missing")
        if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
            return None
        if p.exists() and (max_age_s is None or time.time() - p.stat().st_mtime < max_age_s):
            try:
                txt = p.read_text(encoding="utf-8")
                val = txt if suffix != ".json" else json.loads(txt)
                if max_age_s is None:
                    try:   # kept forever: note the use, so the size cap drops the least recently used first
                        os.utime(p, None)
                    except OSError:
                        pass
                return val
            except (ValueError, OSError):
                pass  # corrupt cache entry: treat as a miss
        val = fn()
        if val is None:
            if suffix != ".json":
                miss.write_text("", encoding="utf-8")  # don't re-ask for missing telemetry for a day
            return None
        _atomic_write(p, val if isinstance(val, str) else json.dumps(val))
        return val

    CSV_CACHE_BYTES = 2 * 1024 ** 3   # saved telemetry kept on disk; the least recently used goes first

    def prune_csv(self, max_bytes: int | None = None, keep=()) -> int:
        """Keep the saved telemetry under max_bytes by deleting the least recently used CSVs (never those for
        the lap ids in keep). Returns how many were deleted. No requests."""
        limit = self.CSV_CACHE_BYTES if max_bytes is None else max_bytes
        keep_files = {hashlib.sha1(("csv/" + str(k)).encode()).hexdigest()[:20] + ".csv" for k in keep}
        files = []
        for f in self.cache.glob("*.csv"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
        total = sum(sz for _, sz, _ in files)
        gone = 0
        for _, sz, f in sorted(files, key=lambda x: x[0]):
            if total <= limit:
                break
            if f.name in keep_files:
                continue
            try:
                f.unlink()
                total -= sz
                gone += 1
            except OSError:
                pass
        return gone

    def peek(self, key: str):
        """A cached JSON answer of any age, or None. Never contacts Garage 61 (used to fill pickers)."""
        p = self.cache / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (ValueError, OSError):
            return None

    @staticmethod
    def peek_saved(token: str, cache_dir: Path, key: str, base_url: str = BASE):
        """Like peek(), without creating a client: no session, no folders created, never a request.
        None if there's no usable token or nothing saved under that key."""
        token = (token or "").strip()
        if not token or token.startswith("PASTE"):
            return None
        base = base_url.rstrip("/") + "/"
        ns = hashlib.sha256((base + "|" + token).encode()).hexdigest()[:10]   # same namespace as __init__
        p = Path(cache_dir) / ns / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except (ValueError, OSError):
            return None

    def has_csv(self, lap_id: str) -> bool:
        """True if this lap's telemetry is already saved (so lap_csv() won't need a request)."""
        return (self.cache / (hashlib.sha1(("csv/" + str(lap_id)).encode()).hexdigest()[:20] + ".csv")).exists()

    def cached_items(self, name: str):
        """Items of a cached list ("teams", "tracks" or "cars") without any request; None if never fetched."""
        if name not in ("teams", "tracks", "cars"):
            raise ValueError(name)
        d = self.peek(name)
        return None if d is None else (d or {}).get("items", [])

    # ---------- endpoints ----------
    def me(self):
        return self._cached("me", lambda: self._get("me"), max_age_s=86400)

    def teams(self):
        d = self._cached("teams", lambda: self._get("teams"), max_age_s=86400)
        return (d or {}).get("items", [])

    def cars(self, refresh=False):
        d = self._cached("cars", lambda: self._get("cars"), max_age_s=0 if refresh else 7 * 86400)
        return (d or {}).get("items", [])

    def tracks(self, refresh=False):
        d = self._cached("tracks", lambda: self._get("tracks"), max_age_s=0 if refresh else 7 * 86400)
        return (d or {}).get("items", [])

    def find_laps(self, fresh: bool = False, **params):
        """GET /laps. Only returns your own, your teammates' and followed drivers' laps.
        fresh=True skips the short-lived cache (used for races you just finished)."""
        key = "laps?" + json.dumps(params, sort_keys=True, default=str)
        d = self._cached(key, lambda: self._get("laps", params), max_age_s=0 if fresh else 600)
        return (d or {}).get("items", [])

    def search_laps_page(self, **params) -> list:
        """GET /laps, never cached: used by the history sync, which keeps its own store of laps
        (so a full history isn't saved twice). One request."""
        return self.laps_page(**params)[0]

    def laps_page(self, **params) -> tuple[list, int | None]:
        """GET /laps, never cached: (items, total). total is Garage 61's count of all matching laps when
        the answer includes one, else None. One request."""
        d = self._get("laps", params)
        if not isinstance(d, dict):
            return [], None
        total = d.get("total")
        try:
            total = int(total) if total is not None and not isinstance(total, bool) else None
        except (TypeError, ValueError):
            total = None
        return list(d.get("items") or []), total

    def my_statistics(self, start_iso: str):
        """Driving statistics (one row per car/track/day). Used to find which tracks to search,
        because the laps search requires a track. Not saved to disk: it's asked for once per sync with a
        different start each time, so a saved copy would never be read again."""
        d = self._get("me/statistics", {"start": start_iso})
        if isinstance(d, dict):
            return d.get("drivingStatistics") or d.get("items") or []
        return d or []

    def csv_missing(self, lap_id: str) -> bool:
        """True if Garage 61 recently said this lap has no telemetry for you (403/404; re-asked after a day)."""
        p = self.cache / (hashlib.sha1(("csv/" + str(lap_id)).encode()).hexdigest()[:20] + ".missing")
        return p.exists() and time.time() - p.stat().st_mtime < 86400

    def lap_csv(self, lap_id: str) -> str | None:
        """Telemetry CSV for one lap. Cached forever (a lap never changes). 403/404 -> None."""
        return self._cached("csv/" + str(lap_id),
                            lambda: self._get(f"laps/{lap_id}/csv", raw=True, forbidden_ok=True),
                            suffix=".csv")
