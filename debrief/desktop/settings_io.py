"""Read and update settings.toml for the desktop Settings screen.

The token is written to settings.toml only. It is never returned to the page, logged,
or kept anywhere else: callers only ever get a short hint (the last 4 characters).
The optional iRacing client id/secret are treated the same way, but with no hint at all:
the page only learns whether each one is saved.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import app, fsutil

PLACEHOLDER_PREFIX = "PASTE"
_TOKEN_OK = re.compile(r"^[\x21-\x7e]{8,4096}$")  # printable ASCII, no spaces
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CRED_OK = re.compile(r"^[\x21-\x7e]{1,512}$")


def _example_text() -> str:
    ex = app.RES / "settings.example.toml"   # next to the code from source; bundled in the installed app
    try:
        return ex.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _raw() -> tuple[dict, str | None]:
    """(parsed settings or {}, error message or None)."""
    p = app.settings_path()
    if not p.exists():
        return {}, None
    try:
        return app._parse_toml(p.read_text(encoding="utf-8-sig")), None
    except SystemExit as e:
        return {}, str(e.code)


def token_present(raw: dict) -> bool:
    t = str(raw.get("token") or "").strip()
    return bool(t) and not t.startswith(PLACEHOLDER_PREFIX)


_NAME_OK = re.compile(r"^[^\x00-\x1f<>]{1,40}$")


def cached_first_name(raw: dict) -> str | None:
    """Your Garage 61 first name from the answer Debrief already saved (Check connection or a review).
    Reads the cache only: it never sends a request. None when unknown."""
    if not token_present(raw):
        return None
    try:
        from ..g61 import BASE, Garage61
        cfg = app.load_settings()
        me = Garage61.peek_saved(cfg.get("token", ""), Path(cfg["cache_dir"]), "me", cfg.get("api_base") or BASE)
    except Exception:  # noqa: BLE001  (a greeting is never worth an error)
        return None
    if not isinstance(me, dict):
        return None
    name = str(me.get("firstName") or me.get("firstname") or "").strip()
    return name if _NAME_OK.match(name) else None


def public_view() -> dict:
    """Everything the Settings screen may show. Never includes the token itself."""
    from .. import __version__
    raw, err = _raw()
    has = token_present(raw)
    out = {
        "version": __version__,
        "installed": app.FROZEN,          # the installed app (True) or run from source (False)
        "data_folder": str(app.HERE),     # where settings.toml, the cache and debrief.log live
        "first_name": cached_first_name(raw) if has else None,
        "file_exists": app.settings_path().exists(),
        "error": err,
        "token_saved": has,
        "token_hint": ("…" + str(raw["token"]).strip()[-4:]) if has else None,
        "max_reference_laps": 3,
        "include_practice": False,
        "sync_on_open": bool(raw.get("sync_on_open", True)),
        "sync_telemetry": "none" if str(raw.get("sync_telemetry", "best")).lower() == "none" else "best",
        "check_updates": bool(raw.get("check_updates", True)),
        "library_folder": None,
        "team_slug": str(raw.get("team_slug") or app.DEFAULT_TEAM).strip().lower() or app.DEFAULT_TEAM,
        # present/absent only: the values are never sent to the page
        "iracing_client_id_saved": bool(str(raw.get("iracing_client_id") or "").strip()),
        "iracing_client_secret_saved": bool(str(raw.get("iracing_client_secret") or "").strip()),
    }
    try:
        cfg = app.load_settings()
        out["max_reference_laps"] = cfg["max_reference_laps"]
        out["include_practice"] = cfg["include_practice"]
        out["library_folder"] = cfg["library_folder"]
    except SystemExit:
        try:
            out["max_reference_laps"] = max(0, min(3, int(raw.get("max_reference_laps", 3))))
        except (TypeError, ValueError):
            pass
        out["include_practice"] = bool(raw.get("include_practice", False))
    return out


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _set_key(text: str, key: str, value) -> str:
    line = f"{key} = {_toml_value(value)}"
    pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _m: line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def save(updates: dict) -> dict:
    """Apply validated updates to settings.toml, keeping every other line as it was.
    Raises ValueError with a friendly message on bad input."""
    clean = {}
    if "token" in updates and updates["token"] is not None and str(updates["token"]).strip():
        tok = str(updates["token"]).strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        if not _TOKEN_OK.match(tok) or tok.startswith(PLACEHOLDER_PREFIX):
            raise ValueError("That doesn't look like a Garage 61 token. Copy it again from "
                             "garage61.net/developer and paste only the token.")
        clean["token"] = tok
    if "max_reference_laps" in updates:
        try:
            n = int(updates["max_reference_laps"])
        except (TypeError, ValueError):
            raise ValueError("Comparison laps must be a number from 0 to 3.")
        if not 0 <= n <= 3:
            raise ValueError("Comparison laps must be a number from 0 to 3.")
        clean["max_reference_laps"] = n
    if "include_practice" in updates:
        v = updates["include_practice"]
        if not isinstance(v, bool):  # bool("false") is True; only accept a real true/false
            raise ValueError("Practice and qualifying must be on or off.")
        clean["include_practice"] = v
    if "sync_on_open" in updates:
        v = updates["sync_on_open"]
        if not isinstance(v, bool):
            raise ValueError("Sync when Debrief opens must be on or off.")
        clean["sync_on_open"] = v
    if "check_updates" in updates:
        v = updates["check_updates"]
        if not isinstance(v, bool):
            raise ValueError("Check for updates must be on or off.")
        clean["check_updates"] = v
    if "sync_telemetry" in updates:
        v = updates["sync_telemetry"]
        if v not in ("best", "none"):
            raise ValueError("Pick whether the sync downloads telemetry.")
        clean["sync_telemetry"] = v
    if "team_slug" in updates:
        slug = str(updates["team_slug"] or "").strip().lower()
        if not _SLUG_OK.match(slug):
            raise ValueError("Pick a team from the list.")
        clean["team_slug"] = slug
    for k, label in (("iracing_client_id", "client ID"), ("iracing_client_secret", "client secret")):
        v = updates.get(k)
        if v is None or not str(v).strip():
            continue  # an empty box means "keep what's saved"
        v = str(v).strip()
        if not _CRED_OK.match(v):
            raise ValueError(f"That iRacing {label} doesn't look right. Paste it again without spaces.")
        clean[k] = v
    if updates.get("clear_iracing") is True:
        clean["iracing_client_id"] = ""
        clean["iracing_client_secret"] = ""
    if not clean:
        raise ValueError("Nothing to save.")

    p = app.settings_path()
    text = p.read_text(encoding="utf-8-sig") if p.exists() else _example_text()
    before_ok = True
    try:
        app._parse_toml(text)
    except SystemExit:
        before_ok = False
    for k, v in clean.items():
        text = _set_key(text, k, v)
    if before_ok:
        app._parse_toml(text)  # raises SystemExit if our edit broke the file (it shouldn't)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    fsutil.replace(tmp, p)
    return public_view()
