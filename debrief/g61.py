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
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests

from . import fsutil

BASE = "https://garage61.net/api/v1/"


class Garage61Error(RuntimeError):
    pass


class Garage61:
    def __init__(self, token: str, cache_dir: Path, min_gap_s: float = 1.0,
                 base_url: str = BASE, log=print):
        token = (token or "").strip()
        if not token or token.startswith("PASTE"):
            raise Garage61Error(
                "No Garage 61 token found. Open settings.toml and paste your token "
                "from garage61.net/developer after token = ")
        u = urlparse(base_url)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
            raise Garage61Error("api_base must use https so your token isn't sent in clear text.")
        self.base = base_url.rstrip("/") + "/"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "User-Agent": "Debrief/1.0 (personal, non-commercial)"})
        # cache is namespaced per API address + token fingerprint (never the token itself)
        ns = hashlib.sha256((self.base + "|" + token).encode()).hexdigest()[:10]
        self.cache = Path(cache_dir) / ns
        self.cache.mkdir(parents=True, exist_ok=True)
        self.min_gap = min_gap_s
        self._last = 0.0
        self.requests_made = 0
        self.log = log

    # ---------- low level ----------
    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self._last = time.monotonic()

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
        return max(1, min(120, int(secs)))

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
        delay = 5
        for attempt in range(5):
            last = attempt == 4
            self._wait()
            self.requests_made += 1
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException as e:
                if last:
                    raise Garage61Error(f"Could not reach Garage 61 ({type(e).__name__}).")
                time.sleep(delay); delay *= 2; continue
            if r.status_code == 401:
                raise Garage61Error("Garage 61 rejected the token (401). Check settings.toml, "
                                    "or create a new token at garage61.net/developer.")
            if r.status_code == 403:
                if forbidden_ok:
                    return None
                raise Garage61Error(f"Garage 61 says this isn't available to your account (403): /{path}. "
                                    "Some data needs a Pro plan or a different privacy setting.")
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if last:
                    break
                wait = self._retry_after(r, delay)
                self.log(f"  Garage 61 asked us to slow down ({r.status_code}); waiting {wait}s")
                time.sleep(wait); delay *= 2; continue
            if not (200 <= r.status_code < 300):
                detail = (r.text or "").strip().replace("\n", " ")[:300]
                raise Garage61Error(f"Garage 61 returned {r.status_code} for /{path}?{urlencode(params)}: {detail}")
            if raw:
                return r.content.decode("utf-8-sig", errors="replace")
            try:
                return r.json()
            except ValueError:
                raise Garage61Error(f"Garage 61 sent something that isn't data for /{path}. Try again later.")
        raise Garage61Error("Garage 61 kept refusing requests; try again later.")

    def _cached(self, key: str, fn, max_age_s: float | None = None, suffix=".json"):
        p = self.cache / (hashlib.sha1(key.encode()).hexdigest()[:20] + suffix)
        miss = self.cache / (p.stem + ".missing")
        if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
            return None
        if p.exists() and (max_age_s is None or time.time() - p.stat().st_mtime < max_age_s):
            try:
                txt = p.read_text(encoding="utf-8")
                return txt if suffix != ".json" else json.loads(txt)
            except (ValueError, OSError):
                pass  # corrupt cache entry: treat as a miss
        val = fn()
        if val is None:
            if suffix != ".json":
                miss.write_text("", encoding="utf-8")  # don't re-ask for missing telemetry for a day
            return None
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(val if isinstance(val, str) else json.dumps(val), encoding="utf-8")
        fsutil.replace(tmp, p)
        return val

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
        d = self._get("laps", params)
        return list((d or {}).get("items") or []) if isinstance(d, dict) else []

    def my_statistics(self, start_iso: str):
        """Driving statistics (one row per car/track/day). Used to find which tracks to search,
        because the laps search requires a track."""
        d = self._cached("stats?" + start_iso[:13], lambda: self._get("me/statistics", {"start": start_iso}), max_age_s=0)
        if isinstance(d, dict):
            return d.get("drivingStatistics") or d.get("items") or []
        return d or []

    def lap_csv(self, lap_id: str) -> str | None:
        """Telemetry CSV for one lap. Cached forever (a lap never changes). 403/404 -> None."""
        return self._cached("csv/" + lap_id,
                            lambda: self._get(f"laps/{lap_id}/csv", raw=True, forbidden_ok=True),
                            suffix=".csv")
