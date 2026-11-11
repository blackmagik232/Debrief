"""Siyanqaba summary (2.30.2, CONTRACT C3): after a session, Debrief can ask Siyanqaba's model on the Zerver to put the
fact sheet's lines in the order a race engineer would say them. Off until Settings > Siyanqaba has an address.

The model only orders: every word and number comes from the fact sheet (debrief/factsheet.py). Lines Debrief marks
required go first and are never the model's choice. Any reply that is not exactly the other lines, each once, is
thrown away and the plain order shows instead. Never used while you are driving (the caller checks), never sends
anything but the fact sheet's lines, and never waits more than TIMEOUT_S.

    plain(clauses, required) -> str
    ordered(address, clauses, required, post=None) -> {"text", "source": "siyanqaba" | "plain", "problems", "model"}
    clean_address(text) -> "http://host:port" (raises ValueError in plain words)
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MODEL = "qwen3:4b-instruct-2507-q4_K_M"     # CONTRACT C3
TIMEOUT_S = 45.0
DEFAULT_PORT = 11434
ORDER_SYSTEM = (
    "You are a race engineer deciding what the driver hears first. You get facts by id. "
    "Return every id exactly once, most important for the driver first. "
    'Return JSON {"order": [ids]}.'
)


def _sentence(c: str) -> str:
    c = str(c).strip()
    return c[:1].upper() + c[1:] + ("" if c.endswith(".") else ".")


def assemble(order, clauses) -> str:
    return " ".join(_sentence(clauses[k]) for k in order)


def plain(clauses: dict, required) -> str:
    """Required lines first, then the rest in the fact sheet's own order."""
    order = [k for k in required or [] if k in clauses] + [k for k in clauses if k not in (required or [])]
    return assemble(order, clauses)


def clean_address(text) -> str:
    """'zerver', '10.0.0.5', '10.0.0.5:11434' or 'http://10.0.0.5:11434' -> 'http://host:port'. Only plain http to a
    machine on the home network (a private address or a bare name): this never goes out to the internet."""
    v = str(text or "").strip()
    if not v:
        return ""
    if len(v) > 200 or re.search(r"[\s\x00-\x1f]", v):
        raise ValueError("That address doesn't look right. Type the Zerver's name or address, like 10.0.0.20.")
    if "://" not in v:
        v = "http://" + v
    u = urlsplit(v)
    if u.scheme != "http" or not u.hostname or u.username or u.password or (u.path not in ("", "/")) or u.query:
        raise ValueError("Type just the Zerver's name or address (and port), like 10.0.0.20 or zerver:11434.")
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_private or ip.is_loopback) or ip.is_unspecified:
            raise ValueError("That address isn't on your home network. Siyanqaba only talks to the Zerver at home.")
    except ValueError as e:
        if "home network" in str(e):
            raise
        if "." in host and not host.lower().endswith((".local", ".lan", ".home", ".internal")):
            raise ValueError("Use the Zerver's name on your network (like zerver) or its address (like 10.0.0.20).")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,62}", host):
            raise ValueError("That address doesn't look right. Type the Zerver's name or address, like 10.0.0.20.")
    try:
        port = u.port or DEFAULT_PORT
    except ValueError:
        raise ValueError("That port doesn't look right. Ollama uses 11434.")
    return f"http://{host}:{port}"


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # home network only: never a proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ordered(address, clauses: dict, required, post=None, timeout: float = TIMEOUT_S) -> dict:
    """Ask the Zerver for the order. Falls back to plain() on anything unexpected; never raises."""
    clauses = {k: str(v) for k, v in (clauses or {}).items() if isinstance(v, str) and v.strip()}
    required = [k for k in required or [] if k in clauses]
    out = {"text": plain(clauses, required), "source": "plain", "problems": [], "model": MODEL}
    rest = {k: v for k, v in clauses.items() if k not in required}
    if not address:
        out["problems"].append("no Siyanqaba address")
        return out
    if len(rest) < 2:
        return out                                   # nothing to order
    try:
        base = clean_address(address)
    except ValueError as e:
        out["problems"].append(str(e))
        return out
    body = {"model": MODEL, "stream": False, "options": {"temperature": 0.0},
            "format": {"type": "object", "properties": {"order": {"type": "array",
                                                                  "items": {"type": "string", "enum": list(rest)}}},
                       "required": ["order"]},
            "messages": [{"role": "system", "content": ORDER_SYSTEM},
                         {"role": "user", "content": "\n".join(f"{k}: {v}" for k, v in rest.items())}]}
    try:
        resp = (post or _post)(base + "/api/chat", body, timeout)
        order = json.loads(((resp or {}).get("message") or {}).get("content") or "{}").get("order")
    except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError) as e:
        out["problems"].append(f"Siyanqaba didn't answer ({type(e).__name__})")
        return out
    if not isinstance(order, list) or sorted(map(str, order)) != sorted(rest):
        out["problems"].append("the order must hold every line exactly once")
        return out
    out.update(text=assemble(required + [str(k) for k in order], clauses), source="siyanqaba")
    return out
