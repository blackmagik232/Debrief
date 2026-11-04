"""Read and update settings.toml for the desktop Settings screen.

The token is written to settings.toml only. It is never returned to the page, logged,
or kept anywhere else: callers only ever get a short hint (the last 4 characters).
The optional iRacing client id/secret are treated the same way, but with no hint at all:
the page only learns whether each one is saved.
The Discord webhook (2.5) is a secret too: its address carries a token. The page gets only
discord_saved and discord_hint ("…1234", the end of the webhook id, never the token), and only
discord_webhook() below hands the address to the server's Discord endpoints.

At rest (2.7.0 "Trust", H3): on Windows both secrets are wrapped with DPAPI (CryptProtectData, tied to
the Windows account) and written as token_dpapi = "<base64>" and discord_webhook_dpapi = "<base64>", so
a copied settings.toml, a backup or a sync folder never holds them in the clear. load_settings() reads
either form (a plaintext token or discord_webhook from an older install still works); the first save
after the upgrade rewrites the plaintext key in the protected form. Off Windows (the tests, a source
checkout on Linux) the plain keys are kept: there is no DPAPI there. See secrets_protected() for the
source-checkout-on-Windows case.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from .. import app, brand, fsutil

PLACEHOLDER_PREFIX = "PASTE"
_TOKEN_OK = re.compile(r"^[\x21-\x7e]{8,4096}$")  # printable ASCII, no spaces
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_CRED_OK = re.compile(r"^[\x21-\x7e]{1,512}$")
MAX_SALUTATION = 20
# discord.WEBHOOK_RE (25-B, §3.1). Used only when the discord module can't be loaded, so a broken module never
# lets a bad address in; discord.valid() is the real check.
_WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d{17,20})/([A-Za-z0-9_-]{60,100})$")
BAD_WEBHOOK = ("That isn't a Discord webhook address. In Discord: channel settings > Integrations > Webhooks > "
               "Copy Webhook URL.")
MAX_FOLDER = 400

# ---------------------------------------------------------------- secrets at rest (DPAPI)
SECRET_KEYS = ("token", "discord_webhook")
DPAPI_SUFFIX = "_dpapi"
_DPAPI_ENTROPY = b"Debrief settings v1"      # ties the blob to Debrief, not just to the Windows account
CRYPTPROTECT_UI_FORBIDDEN = 0x01


def dpapi_available() -> bool:
    """True on Windows, where CryptProtectData exists (ctypes, no extra package)."""
    return os.name == "nt"


def secrets_protected() -> bool:
    """Write secrets in the DPAPI form? Yes in the installed app on Windows (the only way the app runs for
    people), and from source on Windows when DEBRIEF_PROTECT_SECRETS=1. A source checkout keeps the plain
    keys: `python -m debrief` (app.py) reads settings.toml straight and doesn't load this module, so a
    protected token would look missing to it; and the test-suite's settings files are plain by design.
    Reading the protected form works everywhere this module is loaded, whatever this says."""
    if not dpapi_available():
        return False
    return bool(app.FROZEN) or os.environ.get("DEBRIEF_PROTECT_SECRETS") == "1"


def _dpapi_call(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    inp, _keep = blob(data)
    ent, _keep2 = blob(_DPAPI_ENTROPY)
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error() or 0, "DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect_secret(value: str) -> str:
    """A secret as it is written to settings.toml on Windows: base64 of its DPAPI blob. Raises OSError."""
    import base64
    return base64.b64encode(_dpapi_call(True, value.encode("utf-8"))).decode("ascii")


def unprotect_secret(blob_b64: str) -> str | None:
    """The secret back from a *_dpapi value; None if it can't be read (another Windows account, a
    corrupted value, or not Windows at all)."""
    import base64
    try:
        raw = base64.b64decode(str(blob_b64 or "").strip(), validate=True)
        if not raw or not dpapi_available():
            return None
        return _dpapi_call(False, raw).decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError, AttributeError):
        return None


def secret_state(raw: dict, key: str) -> tuple[str, str]:
    """(value, state) of a saved secret. state: "ok" (value read from key_dpapi or the plain key),
    "absent" (neither is saved; value ""), or "unreadable" (a key_dpapi blob is saved but this Windows
    account can't open it, and there is no plain value to fall back on; value ""). 2.7.1 (M1): the Settings
    screen tells "unreadable" apart from "absent" instead of saying no token was ever saved."""
    prot = raw.get(key + DPAPI_SUFFIX)
    blob = isinstance(prot, str) and bool(prot.strip())
    if blob:
        v = unprotect_secret(prot)
        if v is not None and v.strip():
            return v.strip(), "ok"
    v = raw.get(key)
    v = v.strip() if isinstance(v, str) else ""
    if v:
        return v, "ok"
    return "", ("unreadable" if blob else "absent")


def read_secret(raw: dict, key: str) -> str:
    """The saved value of a secret, from its protected form (key_dpapi) first, else the plain key.
    "" when neither is there or the protected one can't be opened (secret_state says which)."""
    return secret_state(raw, key)[0]


def secret_unreadable(raw: dict, key: str) -> bool:
    return secret_state(raw, key)[1] == "unreadable"


_LOGGED_ONCE: set = set()


def _log_once(tag: str, msg: str) -> None:
    """app.say(msg) the first time `tag` comes up in this process (a reload every few seconds stays quiet)."""
    if tag in _LOGGED_ONCE:
        return
    _LOGGED_ONCE.add(tag)
    try:
        app.say(msg)
    except Exception:  # noqa: BLE001  (a log line is never worth an error)
        pass


_UNREADABLE_LOG = {
    "token": "Settings: the saved Garage 61 token (token_dpapi) can't be opened on this Windows account. "
             "Paste it again in Settings.",
    "discord_webhook": "Settings: the saved Discord webhook (discord_webhook_dpapi) can't be opened on this "
                       "Windows account. Paste it again in Settings.",
}


def resolve_secrets(cfg: dict) -> dict:
    """For app.load_settings: fill cfg["token"] / cfg["discord_webhook"] from the protected keys and drop
    the *_dpapi entries, so the rest of the app never sees the difference."""
    for k in SECRET_KEYS:
        if k + DPAPI_SUFFIX in cfg:
            v, state = secret_state(cfg, k)
            cfg.pop(k + DPAPI_SUFFIX, None)
            if v:
                cfg[k] = v
            elif state == "unreadable":
                _log_once("unreadable:" + k, _UNREADABLE_LOG[k])
        if isinstance(cfg.get(k), str):
            app.remember_secret(cfg[k])   # 2.7.1 (M5): never in a shared log excerpt
    return cfg


def _install_loader() -> None:
    """Make app.load_settings() hand out the secrets whichever form they are saved in. app.py itself
    reads TOML only, so the protected keys are resolved here, once, when the desktop app loads this module
    (the server imports it before anything asks for settings). Idempotent."""
    orig = getattr(app, "load_settings", None)
    if orig is None or getattr(orig, "_debrief_resolves_secrets", False):
        return

    def load_settings():
        return resolve_secrets(orig())
    load_settings.__doc__ = orig.__doc__
    load_settings.__wrapped__ = orig
    load_settings._debrief_resolves_secrets = True
    app.load_settings = load_settings


_install_loader()


def expand_folder(v: str) -> str:
    return os.path.expandvars(os.path.expanduser(v))


def clean_team_folder(v) -> str:
    """The team track cards folder: "" (none), or an existing folder (~ and %VARS% expanded). The typed form
    is what's saved, so a OneDrive path written with %USERPROFILE% keeps working. Raises ValueError."""
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("That folder doesn't exist. Pick the shared folder your team uses.")
    v = v.strip().strip('"').strip()
    if not v:
        return ""
    if len(v) > MAX_FOLDER or re.search(r"[\x00-\x1f<>|?*]", v):
        raise ValueError("That folder doesn't exist. Pick the shared folder your team uses.")
    try:
        ok = Path(expand_folder(v)).is_dir()
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise ValueError("That folder doesn't exist. Pick the shared folder your team uses.")
    return v


# ---------------------------------------------------------------- 2.12 Setup Lab and Coach
IRACING_FOLDER_BAD = "That folder doesn't exist. Pick iRacing's folder (the one with telemetry in it) or its telemetry folder."


def telemetry_folder(raw_or_cfg: dict | None = None) -> Path:
    """Where iRacing saves telemetry (.ibt): Documents\\iRacing\\telemetry (the real Documents folder, so a
    OneDrive-moved one is found), or the "iracing_folder" setting: iRacing's own folder (its telemetry folder is
    used) or the telemetry folder itself. Only ever read: Debrief never writes into iRacing's folders."""
    v = str((raw_or_cfg or {}).get("iracing_folder") or "").strip()
    if v:
        p = Path(expand_folder(v))
        try:
            if (p / "telemetry").is_dir():
                return p / "telemetry"
        except (OSError, ValueError):
            pass
        return p
    return app.documents_dir() / "iRacing" / "telemetry"


def clean_iracing_folder(v) -> str:
    """The iracing_folder setting: "" (the default) or an existing folder, saved as typed. Raises ValueError."""
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(IRACING_FOLDER_BAD)
    v = v.strip().strip('"').strip()
    if not v:
        return ""
    if len(v) > MAX_FOLDER or re.search(r"[\x00-\x1f<>|?*]", v):
        raise ValueError(IRACING_FOLDER_BAD)
    try:
        ok = Path(expand_folder(v)).is_dir()
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise ValueError(IRACING_FOLDER_BAD)
    return v


EXTRA_FILES = {"setuplab": "setuplab.json", "coach": "coach.json"}


def _extra_path(name: str) -> Path:
    """Small JSON files next to settings.toml for what doesn't fit its flat keys (Setup Lab's per car + track
    series choices, the coach's settings)."""
    return app.settings_path().parent / EXTRA_FILES[name]


def load_extra(name: str) -> dict:
    """The saved JSON for name, or {} when there is none or it is damaged."""
    try:
        d = json.loads(_extra_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_extra(name: str, data: dict) -> None:
    """Raises OSError when it can't be written."""
    with _SAVE_LOCK:
        fsutil.write_json(_extra_path(name), data, mkdir=True)


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
    t = read_secret(raw, "token")
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


def cached_author_name() -> str | None:
    """The name your shared track cards go out under (notes.author_from_cache: cached, never a request)."""
    try:
        from .. import notes
        fn = getattr(notes, "author_from_cache", None)
        a = fn(app.load_settings()) if fn else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(a, dict) or not a.get("slug"):
        return None
    name = str(a.get("name") or a.get("slug")).strip()
    return name if _NAME_OK.match(name) else str(a.get("slug"))[:40]


def _discord():
    try:
        from .. import discord
        return discord
    except Exception:  # noqa: BLE001  (a missing or broken module: the local rule below)
        return None


def webhook_valid(url) -> bool:
    if not isinstance(url, str) or len(url) > 300:
        return False
    d = _discord()
    if d is not None and callable(getattr(d, "valid", None)):
        try:
            return bool(d.valid(url))
        except Exception:  # noqa: BLE001
            return False
    return bool(_WEBHOOK_RE.match(url)) and not any(c.isspace() for c in url)


def webhook_hint(url: str) -> str:
    """"…/1234": the last 4 digits of the webhook id (discord.hint). Never any part of the token."""
    d = _discord()
    if d is not None and callable(getattr(d, "hint", None)):
        try:
            h = d.hint(url)
            if isinstance(h, str) and h:
                m = _WEBHOOK_RE.match(url or "")
                if not m or m.group(2)[-4:] not in h:   # never pass on anything that could be the token
                    return h[:12]
        except Exception:  # noqa: BLE001
            pass
    m = _WEBHOOK_RE.match(url or "")
    return "…/" + m.group(1)[-4:] if m else "…"


def _saved_webhook(raw: dict) -> str | None:
    v = read_secret(raw, "discord_webhook")
    return v if v and webhook_valid(v) else None


def discord_webhook() -> str | None:
    """The saved webhook address, for the server's Discord endpoints only. Never send it to the page or a log."""
    raw, _err = _raw()
    return _saved_webhook(raw)


def _verification() -> tuple[str, str]:
    """(updater.verification_level(), the Settings > Updates sentence for it)."""
    try:
        from .. import updater
        return updater.verification_level(), updater.verification_note()
    except Exception:  # noqa: BLE001  (never let the updater module keep Settings from opening)
        return "checksum", ("Updates are checked by SHA-256 checksum. Signed updates switch on once a signing "
                            "key is published.")


# ---------------------------------------------------------------- the window's size (2.8)
WINDOW_DEFAULT = (1400, 900)
WINDOW_MIN = (640, 480)
WINDOW_MAX = (7680, 4320)


def _window_file() -> Path:
    return Path(os.environ.get("DEBRIEF_CACHE_DIR") or Path(app.HERE) / ".cache") / "window.json"


def clean_window_size(raw):
    """(width, height) as whole numbers in a sensible range, or None. True/False and text don't count."""
    if not isinstance(raw, dict):
        return None
    w, h = raw.get("width"), raw.get("height")
    if type(w) not in (int, float) or type(h) not in (int, float):
        return None
    if not (WINDOW_MIN[0] <= w <= WINDOW_MAX[0] and WINDOW_MIN[1] <= h <= WINDOW_MAX[1]):
        return None
    return int(w), int(h)


def save_window_size(raw) -> bool:
    """Remember the window's size for the next launch. False if the size makes no sense or can't be saved."""
    size = clean_window_size(raw)
    if size is None:
        return False
    try:
        fsutil.write_json(_window_file(), {"width": size[0], "height": size[1]}, mkdir=True)
    except OSError:
        return False
    return True


def window_size() -> tuple:
    """The size the window was last left at, or the default if there is none or the file is damaged."""
    try:
        return clean_window_size(json.loads(_window_file().read_text(encoding="utf-8"))) or WINDOW_DEFAULT
    except (OSError, ValueError):
        return WINDOW_DEFAULT


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
        # 2.18: Garage 61 said no to the saved token at the last check: the setup steps stay open
        "token_rejected": has and _token_rejected(),
        # 2.7.1 (M1): a token_dpapi is saved but this Windows account can't open it (not "never saved")
        "token_unreadable": secret_unreadable(raw, "token"),
        "discord_unreadable": secret_unreadable(raw, "discord_webhook"),
        "token_hint": ("…" + read_secret(raw, "token")[-4:]) if has else None,
        # 2.7.0: how the token and webhook are kept in settings.toml ("dpapi" on Windows, "plain" elsewhere)
        "secrets_at_rest": "dpapi" if secrets_protected() else "plain",
        "max_reference_laps": 3,
        "include_practice": False,
        "sync_on_open": bool(raw.get("sync_on_open", True)),
        "sync_telemetry": "none" if str(raw.get("sync_telemetry", "best")).lower() == "none" else "best",
        "check_updates": bool(raw.get("check_updates", True)),
        "salutation": clean_salutation(raw.get("salutation")),
        # 2.18 Settings > Units: speeds in mph (default) or km/h on every page and in the coach's voice
        "units": clean_units(raw.get("units")),
        "library_folder": None,
        "team_slug": str(raw.get("team_slug") or app.DEFAULT_TEAM).strip().lower() or app.DEFAULT_TEAM,
        # 2.21: the team's own name and members (Midfield Menace), apart from the Garage 61 group (team_slug)
        "team_name": " ".join(str(raw.get("team_name") or brand.TEAM_NAME).split())[:60] or brand.TEAM_NAME,
        "team_members": brand.parse_members(raw.get("team_members")),
        # present/absent only: the values are never sent to the page
        "iracing_client_id_saved": bool(str(raw.get("iracing_client_id") or "").strip()),
        "iracing_client_secret_saved": bool(str(raw.get("iracing_client_secret") or "").strip()),
        # 2.4 team track cards (a folder path is not a secret)
        "team_cards_folder": str(raw.get("team_cards_folder") or "").strip()[:MAX_FOLDER] or None,
        "team_cards_folder_ok": False,
        # 2.7.5 RaceCap link: the library folder RaceCap uses. Only its Races folder is ever read.
        "racecap_library": str(raw.get("racecap_library") or "").strip()[:MAX_FOLDER] or None,
        "share_my_cards": raw.get("share_my_cards") is True,
        # 2.12 Setup Lab and Coach: where iRacing saves telemetry (.ibt). Empty = Documents\iRacing\telemetry.
        "iracing_folder": str(raw.get("iracing_folder") or "").strip()[:MAX_FOLDER] or None,
        "telemetry_folder": str(telemetry_folder(raw)),
        "telemetry_folder_ok": False,
        # 2.5 Discord webhook: saved or not, and the end of the webhook id. Never the address or its token.
        "discord_saved": False,
        "discord_hint": None,
        # 2.7.0 (H1): what an update is checked against before it installs, in the window's own words
        "verification_level": _verification()[0],
        "verification_note": _verification()[1],
    }
    hook = _saved_webhook(raw)
    if hook:
        out["discord_saved"] = True
        out["discord_hint"] = webhook_hint(hook)
    try:
        out["telemetry_folder_ok"] = Path(out["telemetry_folder"]).is_dir()
    except (OSError, ValueError):
        pass
    out["share_as"] = None
    if out["team_cards_folder"]:
        try:
            out["team_cards_folder_ok"] = Path(expand_folder(out["team_cards_folder"])).is_dir()
        except (OSError, ValueError):
            pass
        if out["share_my_cards"] and has:
            out["share_as"] = cached_author_name()
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


def clean_units(v) -> str:
    """"mph" (the default) or "kmh". Anything odd in a hand-edited file reads as the default."""
    from .. import units as _u
    return "kmh" if isinstance(v, str) and _u.norm(v) == "kmh" else "mph"


_UNITS_CACHE = {"key": None, "val": "mph"}


def units() -> str:
    """The saved speed unit, read again only when settings.toml changes (the window asks on every reply)."""
    p = app.settings_path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return "mph"
    if _UNITS_CACHE["key"] == key:
        return _UNITS_CACHE["val"]
    raw, _err = _raw()
    val = clean_units(raw.get("units"))
    _UNITS_CACHE.update(key=key, val=val)
    return val


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
_LABEL = {"token": "Garage 61 token", "discord_webhook": "Discord webhook"}


def _protect_writes(clean: dict, before: dict) -> dict:
    """The lines to write for the validated updates. On Windows a secret goes out as key_dpapi (its DPAPI
    blob) and the plain key is blanked; a plain secret left by an older version is rewritten the same way
    on this save, whatever was changed. Off Windows the plain keys are written as before."""
    out = dict(clean)
    if not secrets_protected():
        return out
    for k in SECRET_KEYS:
        if k in out:
            v = out[k]
            if v:
                try:
                    out[k + DPAPI_SUFFIX] = protect_secret(v)
                except OSError:
                    # DPAPI unavailable (an odd account): keep the plain line, as before 2.7, and blank any
                    # older blob, which read_secret would otherwise prefer over the value just pasted
                    # (2.7.1, M2: the save then failed its read-back check)
                    out[k + DPAPI_SUFFIX] = ""
                    _log_once("protect:" + k, f"Settings: Windows couldn't encrypt the {_LABEL[k]} (DPAPI), so "
                              "it is saved in settings.toml in the clear. Keep that file private.")
                    continue
                out[k] = ""
            else:
                out[k + DPAPI_SUFFIX] = ""       # cleared: both forms go
        elif isinstance(before.get(k), str) and before.get(k).strip() and \
                not str(before.get(k)).startswith(PLACEHOLDER_PREFIX):
            try:
                out[k + DPAPI_SUFFIX] = protect_secret(str(before[k]).strip())
            except OSError:
                continue
            out[k] = ""
    return out


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
    if "units" in updates:
        v = updates["units"]
        if v not in ("mph", "kmh"):
            raise ValueError("Pick mph or km/h for speeds.")
        clean["units"] = v
    if "team_slug" in updates:
        slug = str(updates["team_slug"] or "").strip().lower()
        if not _SLUG_OK.match(slug):
            raise ValueError("Pick a team from the list.")
        clean["team_slug"] = slug
    if "team_name" in updates:
        v = " ".join(str(updates["team_name"] or "").split())
        if not v or len(v) > 60 or re.search(r"[\x00-\x1f\x7f<>]", v):
            raise ValueError("Give the team a short name (up to 60 characters, no < or >).")
        clean["team_name"] = v
    if "team_members" in updates:
        v = updates["team_members"]
        names = brand.parse_members(v if isinstance(v, (list, str)) else "")
        if any(re.search(r"[\x00-\x1f\x7f<>]", n) for n in names):
            raise ValueError("Member names can't contain < or >.")
        clean["team_members"] = "; ".join(names)
    for k, label in (("iracing_client_id", "client ID"), ("iracing_client_secret", "client secret")):
        v = updates.get(k)
        if v is None or not str(v).strip():
            continue  # an empty box means "keep what's saved"
        v = str(v).strip()
        if not _CRED_OK.match(v):
            raise ValueError(f"That iRacing {label} doesn't look right. Paste it again without spaces.")
        clean[k] = v
    if "racecap_library" in updates:
        v = str(updates["racecap_library"] or "").strip()
        if len(v) > MAX_FOLDER or re.search(r"[\x00-\x1f\x7f]", v):
            raise ValueError("That folder path doesn't look right.")
        if v:
            from .. import racecap_link
            if racecap_link.races_root(v) is None:
                raise ValueError("That folder has no Races folder from RaceCap. Pick the folder RaceCap shows under Tools > Library.")
        clean["racecap_library"] = v
    if "team_cards_folder" in updates:
        clean["team_cards_folder"] = clean_team_folder(updates["team_cards_folder"])
    if "iracing_folder" in updates:
        clean["iracing_folder"] = clean_iracing_folder(updates["iracing_folder"])
    if "share_my_cards" in updates:
        v = updates["share_my_cards"]
        if not isinstance(v, bool):
            raise ValueError("Sharing your track cards must be on or off.")
        clean["share_my_cards"] = v
    if "discord_webhook" in updates:
        v = updates["discord_webhook"]
        bad = getattr(_discord(), "MSG_BAD_URL", None)
        bad = bad if isinstance(bad, str) and bad else BAD_WEBHOOK
        if v is not None and not isinstance(v, str):
            raise ValueError(bad)
        v = (v or "").strip()
        if v:   # an empty box means "keep what's saved" (Remove is clear_discord)
            if not webhook_valid(v):
                raise ValueError(bad)
            clean["discord_webhook"] = v
    if updates.get("clear_discord") is True:
        clean["discord_webhook"] = ""
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
            before = app._parse_toml(text)
        except SystemExit:
            before_ok, before = False, {}
        for k in SECRET_KEYS:   # 2.7.1 (M5): the value being replaced, and the new one, stay out of shared logs
            if k in clean:
                app.remember_secret(read_secret(before, k) if before_ok else "")
                app.remember_secret(clean[k])
        lines = _protect_writes(clean, before if before_ok else {})
        for k, v in lines.items():
            text = _set_key(text, k, v)
        if before_ok:
            after = app._parse_toml(text)  # raises SystemExit if our edit broke the file (it shouldn't)
            # every change must read back as a top-level setting with the new value, or nothing is saved
            if any(after.get(k) != v for k, v in lines.items()) or \
                    any(read_secret(after, k) != v for k, v in clean.items() if k in SECRET_KEYS):
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
    if clean.get("token"):
        _token_check_clear()   # 2.18: a new token hasn't been turned down yet
    return public_view()


def _token_rejected() -> bool:
    """2.18: Garage 61 rejected the saved token at the last connection check (firstrun.json). Never raises."""
    try:
        from .. import firstrun
        return firstrun.token_rejected()
    except Exception:  # noqa: BLE001
        return False


def _token_check_clear():
    try:
        from .. import firstrun
        if firstrun.token_rejected():
            firstrun.mark("token_rejected", value=False)
    except Exception:  # noqa: BLE001
        pass
