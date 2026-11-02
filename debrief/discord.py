"""Post a review's share card to a Discord channel through a webhook, only when the driver asks.

    valid(url) -> bool              is this a Discord webhook address (no query, fragment or spaces)?
    hint(url) -> str                "…/1234" (last 4 digits of the webhook id); never the token
    redact(text, url=None) -> str   text with any webhook token replaced by "…"
    check(url) -> dict              GET the webhook: {"ok": True, "name", "channel_id"}   (1 Discord request)
    post(url, png, content) -> dict {"ok": True, "id": message id or None}             (1 Discord request)
    DiscordError                    a plain message to show as-is (never contains the webhook token)

Request budget (spec 3.7): Discord post = 1 Discord request per confirmed post (plus one retry only when
Discord answers 429 with Retry-After <= 10 s); Discord check = 1 Discord request per click; 0 Garage 61.

    digest(rows, today=None) -> dict the weekly digest DRAFT from library rows: text only, it sends nothing

Nothing in this module runs on import, on a timer or from a job: it only acts when server.py calls it
after the driver presses Post (or Check) in Settings / the preview dialog. The webhook address is a
secret (anyone with it can post to the channel): it is never logged, and every error message is
redacted. The only log line is "Posted to Discord (webhook …1234)".
"""
from __future__ import annotations

import json
import logging
import re
import struct
import time

WEBHOOK_RE = r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d{17,20})/([A-Za-z0-9_-]{60,100})$"
_WEBHOOK = re.compile(WEBHOOK_RE)
# any webhook-shaped path anywhere in a string (for redaction, so it is deliberately looser)
_ANY_TOKEN = re.compile(r"(/api/(?:v\d+/)?webhooks/\d{5,25}/)[A-Za-z0-9_.\-]{20,200}")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PNG = 4 * 1024 * 1024
MAX_SIDE = 2400
MAX_CONTENT = 2000
MAX_RETRY_WAIT_S = 10.0
TIMEOUT_S = 20

MSG_BAD_URL = ("That isn't a Discord webhook address. In Discord: channel settings > Integrations > "
               "Webhooks > Copy Webhook URL.")
MSG_GONE = "That Discord webhook no longer works. Paste a new one in Settings."
MSG_TOO_BIG = "The picture is too big for Discord."
MSG_WAIT = "Discord asked Debrief to wait. Try again in a minute."
MSG_NET = "Couldn't reach Discord."
MSG_NOT_PNG = "The share card isn't a PNG picture, so it wasn't posted."
MSG_TOO_LONG = f"The message is too long for Discord ({MAX_CONTENT} characters at most)."

_log = logging.getLogger("debrief.discord")


class DiscordError(Exception):
    """A plain problem to show as-is. The message never contains the webhook token."""


def _parts(url):
    if not isinstance(url, str) or any(c.isspace() for c in url) or "?" in url or "#" in url:
        return None
    m = _WEBHOOK.fullmatch(url)
    return (m.group(1), m.group(2)) if m else None


def valid(url: str) -> bool:
    return _parts(url) is not None


def hint(url: str) -> str:
    p = _parts(url)
    return "…/" + p[0][-4:] if p else ""


def redact(text, url=None) -> str:
    """text with the webhook token (of url, and any webhook-shaped token) replaced by '…'."""
    s = str(text)
    p = _parts(url) if url else None
    if p:
        s = s.replace(p[1], "…")
    return _ANY_TOKEN.sub(lambda m: m.group(1) + "…", s)


class _RedactFilter(logging.Filter):
    """Rewrites log records of the HTTP libraries so a webhook token never reaches a log file
    (urllib3 logs request paths at DEBUG level)."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


_FILTER = _RedactFilter()


def _guard_logs():
    # called from check()/post() only (never on import)
    for name in ("urllib3.connectionpool", "urllib3", "requests"):
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)


def _headers():
    try:
        from . import __version__
    except ImportError:
        __version__ = "?"
    return {"User-Agent": f"Debrief/{__version__} (share card)"}


def _png_size(png):
    """(width, height) from a PNG's IHDR, or None if it isn't a PNG."""
    if not isinstance(png, (bytes, bytearray)) or len(png) < 33 or not bytes(png[:8]) == PNG_MAGIC:
        return None
    ln, kind = struct.unpack(">I4s", bytes(png[8:16]))
    if kind != b"IHDR" or ln != 13:
        return None
    return struct.unpack(">II", bytes(png[16:24]))


_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_MASS_MENTION = re.compile(r"@+(?=everyone|here)", re.I)


def clean_content(content) -> str:
    """The message text as it is sent: control characters taken out (C0, DEL and C1; line breaks are kept,
    \r\n and \r become \n, a tab becomes a space), then the @ of every @everyone / @here taken out, again
    and again until nothing changes, so '@@everyone' or '@every@everyoneone' can't leave a mention behind.
    Mentions are also neutered by allowed_mentions."""
    s = "" if content is None else str(content)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    s = _CONTROL.sub("", s)      # first, so '@\x00everyone' can't join up into a mention afterwards
    while True:
        t = _MASS_MENTION.sub("", s)
        if t == s:
            break
        s = t
    return s.strip()


def _retry_after(r) -> float | None:
    v = None
    try:
        v = (r.headers or {}).get("Retry-After")
    except AttributeError:
        v = None
    if v is None:
        try:
            js = r.json()
            v = js.get("retry_after") if isinstance(js, dict) else None
        except Exception:
            v = None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v and v >= 0 else None


def _status_error(code: int) -> DiscordError:
    if code in (401, 403, 404):
        return DiscordError(MSG_GONE)
    if code == 413:
        return DiscordError(MSG_TOO_BIG)
    if code == 429:
        return DiscordError(MSG_WAIT)
    return DiscordError(f"Discord returned {int(code)}. Try again later.")


def _session(session):
    if session is not None:
        return session
    import requests
    return requests


def _send(fn):
    """Run one request; any network problem becomes a plain DiscordError with no URL in it (and no
    chained exception, whose text would carry the URL)."""
    try:
        return fn()
    except Exception:
        pass
    raise DiscordError(MSG_NET)


def check(url: str, session=None) -> dict:
    """GET the webhook (1 Discord request): {"ok": True, "name", "channel_id"} for Settings."""
    if not valid(url):
        raise DiscordError(MSG_BAD_URL)
    _guard_logs()
    s = _session(session)
    r = _send(lambda: s.get(url, headers=_headers(), timeout=TIMEOUT_S))
    code = int(getattr(r, "status_code", 0) or 0)
    if code != 200:
        raise _status_error(code)
    try:
        js = r.json()
    except Exception:
        js = None
    if not isinstance(js, dict):
        raise DiscordError("Discord sent something unexpected. Try again later.")
    name = js.get("name")
    ch = js.get("channel_id")
    # only these two fields: the reply also carries the token, which must never reach the page
    return {"ok": True, "name": str(name)[:100] if name is not None else None,
            "channel_id": str(ch) if ch is not None and re.fullmatch(r"\d{1,25}", str(ch)) else None}


def post(url: str, png: bytes, content: str, session=None, sleep=time.sleep) -> dict:
    """Post the PNG with a message (1 Discord request). Everything is checked before any network call.
    Raises DiscordError(plain message)."""
    if not valid(url):
        raise DiscordError(MSG_BAD_URL)
    if not isinstance(png, (bytes, bytearray)) or not bytes(png[:8]) == PNG_MAGIC:
        raise DiscordError(MSG_NOT_PNG)
    if len(png) > MAX_PNG:
        raise DiscordError(MSG_TOO_BIG)
    size = _png_size(png)
    if size is None:
        raise DiscordError(MSG_NOT_PNG)
    w, h = size
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
        raise DiscordError(MSG_TOO_BIG)
    text = clean_content(content)
    if len(text) > MAX_CONTENT:
        raise DiscordError(MSG_TOO_LONG)
    _guard_logs()
    s = _session(session)
    payload = json.dumps({"content": text, "allowed_mentions": {"parse": []}})
    png = bytes(png)

    def once():
        return _send(lambda: s.post(url + "?wait=true", headers=_headers(),
                                    files={"files[0]": ("debrief.png", png, "image/png")},
                                    data={"payload_json": payload}, timeout=TIMEOUT_S))

    r = once()
    code = int(getattr(r, "status_code", 0) or 0)
    if code == 429:
        wait = _retry_after(r)
        if wait is None or wait > MAX_RETRY_WAIT_S:
            raise DiscordError(MSG_WAIT)
        sleep(wait)
        r = once()
        code = int(getattr(r, "status_code", 0) or 0)
    if code not in (200, 204):
        raise _status_error(code)
    mid = None
    if code == 200:
        try:
            js = r.json()
            mid = str(js.get("id")) if isinstance(js, dict) and js.get("id") is not None else None
        except Exception:
            mid = None
    _log.info("Posted to Discord (webhook %s)", "…" + _parts(url)[0][-4:])
    return {"ok": True, "id": mid}


# ---------------------------------------------------------------- weekly digest draft (2.8)
DIGEST_DAYS = 7
DIGEST_MAX_LINES = 12


def _lap(t) -> str:
    m = int(t // 60)
    return f"{m}:{t - m * 60:06.3f}"


def _short_day(d) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _digest_line(r) -> str:
    bits = [f"{r.get('track_short') or r.get('track') or 'Unknown track'}, {r.get('car') or 'unknown car'}"]
    if isinstance(r.get("best"), (int, float)) and r.get("best_clean", True):
        bits.append(f"best {_lap(r['best'])}")
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    rc = r.get("racecap") if isinstance(r.get("racecap"), dict) else {}
    if res.get("finish_pos"):
        bits.append(f"P{int(res['finish_pos'])}" + (f" from P{int(res['start_pos'])}" if res.get("start_pos") else ""))
    elif rc.get("finish"):
        bits.append(f"P{rc['finish']}" + ("" if rc.get("official") else " on the road"))
    if isinstance(r.get("pb"), dict) and r["pb"].get("is_pb"):
        bits.append("new PB")
    return "- " + ": ".join(bits[:2]) + (", " + ", ".join(bits[2:]) if len(bits) > 2 else "")


def digest(rows, today=None) -> dict:
    """The last 7 days of race reviews as a message to read and edit. THIS ONLY MAKES TEXT. Posting stays with the
    window's preview dialog and its Post button (server.py's api/discord-post), so a timer or a page can't post it.
    Practice, qualifying and test data are left out. {"races": 0, "text": ""} when there's nothing this week."""
    from datetime import date, timedelta
    from . import library
    end = today or date.today()
    start = end - timedelta(days=DIGEST_DAYS - 1)
    week = [r for r in rows or [] if isinstance(r, dict) and library.is_race(r) and not r.get("test_data")
            and start.isoformat() <= str(r.get("date") or "") <= end.isoformat()]
    week.sort(key=lambda r: (str(r.get("date")), str(r.get("started_at"))))
    out = {"races": len(week), "from": start.isoformat(), "to": end.isoformat(), "text": "", "lines": [],
           "pbs": sum(1 for r in week if isinstance(r.get("pb"), dict) and r["pb"].get("is_pb"))}
    if not week:
        return out
    who = next((str(r["driver"]) for r in reversed(week) if r.get("driver")), "")
    n = len(week)
    head = (who + ": " if who else "") + f"{n} race{'' if n == 1 else 's'} from {_short_day(start)} to {_short_day(end)}"
    lines = [_digest_line(r) for r in week]
    out["lines"] = lines
    tail = []
    if out["pbs"]:
        tail.append(f"New personal bests: {out['pbs']}.")
    ir = [r["irating"] for r in week if isinstance(r.get("irating"), int)]
    if len(ir) >= 2 and ir[0] != ir[-1]:
        tail.append(f"iRating {ir[0]} to {ir[-1]}.")
    from . import profile
    scanned, _ = profile.scanned_rows(week)
    if scanned:
        made = sum(r["racecap"]["made"] for r in scanned)
        lost = sum(r["racecap"]["lost"] for r in scanned)
        c = len(scanned)
        tail.append(f"Replay scan of {c} race{'' if c == 1 else 's'}: {made} on-track passes made, {lost} suffered "
                    "(only cars near the replay camera).")
    # always a whole message: fewer race lines rather than a sentence cut off at Discord's limit
    keep = min(len(lines), DIGEST_MAX_LINES)
    while True:
        more = [f"...and {len(lines) - keep} more"] if keep < len(lines) else []
        text = clean_content("\n".join([head] + lines[:keep] + more + tail))
        if len(text) <= MAX_CONTENT or keep == 0:
            break
        keep -= 1
    out["text"] = text[:MAX_CONTENT]
    return out
