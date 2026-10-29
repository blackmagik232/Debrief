"""Read and update settings.toml for the desktop Settings screen.

The token is written to settings.toml only. It is never returned to the page, logged,
or kept anywhere else: callers only ever get a short hint (the last 4 characters).
The optional iRacing client id/secret are treated the same way, but with no hint at all:
the page only learns whether each one is saved.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from .. import app, fsutil

PLACEHOLDER_PREFIX = "PASTE"
_TOKEN_OK = re.compile(r"^[\x21-\x7e]{8,4096}$")  # printable ASCII, no spaces
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CRED_OK = re.compile(r"^[\x21-\x7e]{1,512}$")
MAX_SALUTATION = 20


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
        "salutation": clean_salutation(raw.get("salutation")),
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


def clean_salutation(v) -> str:
    """How reviews address you (e.g. "sir"). "" = no salutation. Anything odd in a hand-edited file reads as ""."""
    if not isinstance(v, str):
        return ""
    v = " ".join(v.split())
    return v if len(v) <= MAX_SALUTATION and not re.search(r"[\x00-\x1f\x7f<>]", v) else ""


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _line_states(lines: list[str]) -> list[bool]:
    """For each line: does it start outside any string (True), or inside a multi-line string (False)?
    A small TOML scanner: enough to know where real keys and [table] headers are."""
    out, state = [], None   # None | '"""' | "'''"
    for ln in lines:
        out.append(state is None)
        i, n = 0, len(ln)
        while i < n:
            if state is None:
                c = ln[i]
                if c == "#":
                    break
                if ln.startswith('"""', i) or ln.startswith("'''", i):
                    state = ln[i:i + 3]
                    i += 3
                    continue
                if c == '"':          # one-line basic string: skip to its closing quote
                    i += 1
                    while i < n and ln[i] != '"':
                        i += 2 if ln[i] == "\\" else 1
                    i += 1
                    continue
                if c == "'":         # one-line literal string
                    j = ln.find("'", i + 1)
                    i = n if j < 0 else j + 1
                    continue
                i += 1
            else:
                if state == '"""' and ln[i] == "\\":
                    i += 2
                    continue
                if ln.startswith(state, i):
                    i += 3
                    while i < n and ln[i] == state[0]:   # up to two extra quotes belong to the string
                        i += 1
                    state = None
                    continue
                i += 1
    return out


def _set_key(text: str, key: str, value) -> str:
    """Set a top-level key. Only real top-level lines count: never a line inside a [table] or inside a
    multi-line string. A new key goes before the first [table], so TOML reads it as top-level."""
    line = f"{key} = {_toml_value(value)}"
    lines = text.splitlines(keepends=True)
    states = _line_states(lines)
    pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")
    first_table = len(lines)
    for i, (ln, free) in enumerate(zip(lines, states)):
        if free and ln.lstrip().startswith("["):
            first_table = i
            break
    for i in range(first_table):
        if states[i] and pat.match(lines[i]):
            j = i + 1   # a multi-line value: replace its continuation lines too
            while j < len(lines) and not states[j]:
                j += 1
            end = "\n" if lines[j - 1].endswith("\n") or j < len(lines) else ""
            lines[i:j] = [line + end]
            return "".join(lines)
    if first_table == len(lines):
        if text and not text.endswith("\n"):
            text += "\n"
        return text + line + "\n"
    head = lines[:first_table]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    # the new key goes after the last top-level line, before any blank lines or comments that lead into the table
    k = len(head)
    while k > 0 and (not head[k - 1].strip() or head[k - 1].lstrip().startswith("#")) and states[k - 1]:
        k -= 1
    return "".join(head[:k]) + line + "\n" + "".join(head[k:]) + "".join(lines[first_table:])


_SAVE_LOCK = threading.Lock()


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
    if "salutation" in updates:
        v = updates["salutation"]
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValueError("The salutation must be text.")
        v = " ".join(v.split())
        if len(v) > MAX_SALUTATION:
            raise ValueError(f"Keep the salutation short: up to {MAX_SALUTATION} characters.")
        if re.search(r"[\x00-\x1f\x7f<>]", v):
            raise ValueError("The salutation can't contain < or >.")
        clean["salutation"] = v
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
    with _SAVE_LOCK:   # one read-modify-write at a time
        text = p.read_text(encoding="utf-8-sig") if p.exists() else _example_text()
        before_ok = True
        try:
            app._parse_toml(text)
        except SystemExit:
            before_ok = False
        for k, v in clean.items():
            text = _set_key(text, k, v)
        if before_ok:
            after = app._parse_toml(text)  # raises SystemExit if our edit broke the file (it shouldn't)
            # every change must read back as a top-level setting with the new value, or nothing is saved
            if any(after.get(k) != v for k, v in clean.items()):
                raise ValueError("Debrief couldn't save that safely, because settings.toml has been edited "
                                 "by hand in an unusual way. Open settings.toml and change it there, or "
                                 "delete it to start again.")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            fsutil.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return public_view()
