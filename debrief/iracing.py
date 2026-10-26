"""OPTIONAL iRacing results connector -- UNTESTED SCAFFOLD. Nothing in Debrief calls this by default.

Why it exists: iRacing removed password login for its data API (December 2025). The data API now needs
OAuth client credentials that iRacing issues on request to individual developers. If Swazi ever gets
credentials, this module is where official results (finish position, incidents, iRating/SR change)
could be fetched instead of typing them in (see results.py, which this would feed).

STATUS: UNTESTED. It has never been run against iRacing. The endpoint paths and parameters below follow
iRacing's published OAuth documentation as best understood, but must be checked against the docs that
come with the credentials before use. Do not wire this into anything that runs automatically.

Settings (settings.toml, both optional):
    iracing_client_id     = "..."
    iracing_client_secret = "..."     # treat like the Garage 61 token: never log, show or write it

Flow (authorization code + PKCE, run only when the user clicks something):
  1. start_authorization(cfg)  -> (url, state, verifier). Open url in the browser; the user logs in at
     oauth.iracing.com and is redirected to REDIRECT_URI with ?code=..&state=..
  2. exchange_code(cfg, code, verifier)  -> token dict (access_token, refresh_token, expires_in)
  3. get_result(token, subsession_id)  -> GET members-ng.iracing.com/data/results/get?subsession_id=..
     The data API answers with {"link": "<signed S3 url>"}; the real JSON is one more GET to that link.
  4. to_result_fields(result_json, cust_id) -> dict for results.save()
Requests: 1 (token) + 2 per result (data API + signed link). Tokens are kept in memory only.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

AUTH_URL = "https://oauth.iracing.com/oauth2/authorize"
TOKEN_URL = "https://oauth.iracing.com/oauth2/token"
DATA_BASE = "https://members-ng.iracing.com/data/"
REDIRECT_URI = "http://127.0.0.1:8765/iracing/callback"  # UNTESTED: must match what iRacing registers
SCOPE = "iracing.auth"  # UNTESTED: confirm the scope name in iRacing's docs


def available(cfg: dict) -> tuple[bool, str]:
    """(True, "") only when both iracing_client_id and iracing_client_secret are in settings."""
    cid = str((cfg or {}).get("iracing_client_id") or "").strip()
    sec = str((cfg or {}).get("iracing_client_secret") or "").strip()
    if not cid or not sec:
        return False, ("iRacing results need API credentials that iRacing issues on request. "
                       "Without them, add race results by hand from a review.")
    return True, ""


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_authorization(cfg: dict) -> tuple[str, str, str]:
    """UNTESTED. Returns (url_to_open, state, code_verifier). Keep state+verifier in memory only."""
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(why)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    q = {"response_type": "code", "client_id": cfg["iracing_client_id"], "redirect_uri": REDIRECT_URI,
         "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return AUTH_URL + "?" + urlencode(q), state, verifier


def exchange_code(cfg: dict, code: str, verifier: str) -> dict:
    """UNTESTED. Swap the authorization code for tokens (1 request). Never log the response."""
    import requests
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(why)
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": cfg["iracing_client_id"], "client_secret": cfg["iracing_client_secret"],
        "code_verifier": verifier})
    if r.status_code != 200:
        raise RuntimeError(f"iRacing login failed ({r.status_code}).")  # no body: it may echo secrets
    return r.json()


def get_result(token: dict, subsession_id: int) -> dict:
    """UNTESTED. Official result for one subsession (2 requests: data API + signed link)."""
    import requests
    h = {"Authorization": f"Bearer {token['access_token']}"}
    r = requests.get(DATA_BASE + "results/get", params={"subsession_id": int(subsession_id)}, headers=h, timeout=30)
    r.raise_for_status()
    link = (r.json() or {}).get("link")
    if not link:
        raise RuntimeError("iRacing didn't return a results link.")
    r2 = requests.get(link, timeout=30)  # signed URL: no Authorization header
    r2.raise_for_status()
    return r2.json()


def to_result_fields(res: dict, cust_id: int) -> dict | None:
    """UNTESTED. Map iRacing's result JSON to results.save() fields for one driver (race session only).
    Field names follow the data API's documented shape (session_results[].results[])."""
    for sess in res.get("session_results") or []:
        if str(sess.get("simsession_type_name", "")).lower() != "race":
            continue
        rows = sess.get("results") or []
        for row in rows:
            if row.get("cust_id") != cust_id:
                continue
            sr = lambda v: round(v / 100.0, 2) if isinstance(v, (int, float)) else None  # 399 -> 3.99
            return {"start_pos": (row.get("starting_position") or 0) + 1,
                    "finish_pos": (row.get("finish_position") or 0) + 1,
                    "field_size": len(rows), "incidents": row.get("incidents"),
                    "irating_before": row.get("oldi_rating"), "irating_after": row.get("newi_rating"),
                    "sr_before": sr(row.get("old_sub_level")), "sr_after": sr(row.get("new_sub_level")),
                    "sof": res.get("event_strength_of_field"), "laps_led": row.get("laps_lead")}
    return None
