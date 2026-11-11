"""2.30.2 Siyanqaba in the window: "How did it feel" taps, the night shift folder and Siyanqaba's summary, all on a
practice session's page.

    GET  api/siyanqaba/session?id=   taps, corners to tap, whether the session reached the Zerver, the plain summary
    GET  api/siyanqaba/summary?id=   the summary in Siyanqaba's order (the Zerver's model), or the plain one
    POST api/feel        {id, corner, apex_pct, tag, phase}   one tap
    POST api/feel/undo   {id, index?}                          take one back (the last one by default)
    POST api/zerver/push {id}                                  send this session to the night shift folder now

Auto-send: opening the session list sends, in the background, any session from the last week that hasn't reached the
night shift folder yet (zerverpush.AUTO_DAYS), so a session is sent the next time Debrief is opened after a drive.
Nothing here runs while you're driving: the summary is refused while the coach or the recorder has a live session.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from .. import app

GET_ROUTES = ("api/siyanqaba/session", "api/siyanqaba/summary")
POST_ROUTES = ("api/feel", "api/feel/undo", "api/zerver/push")
SENT_FILE = "zerver-sent.json"
_auto = {"busy": False, "last": None}
_auto_lock = threading.Lock()


def _mods():
    from .. import aisummary, c1, factsheet, feel, zerverpush
    return aisummary, c1, factsheet, feel, zerverpush


def sent_file(cfg) -> Path:
    return Path(cfg["cache_dir"]) / SENT_FILE


def _store(cfg):
    from .. import feel
    return feel.FeelStore(feel.folder_for(cfg["library_folder"]))


def _session(h, q):
    cfg = h._cfg()
    me, ss, idx = h._practice_session(cfg, q)
    return cfg, me, ss, idx


def _corners(cards: dict) -> tuple:
    info = (cards or {}).get("info") or {}
    try:
        L = float(info.get("length_m") or 0)
    except (TypeError, ValueError):
        L = 0.0
    out = []
    for c in (cards or {}).get("corners") or []:
        a = c.get("apex_m")
        out.append({"id": c.get("id"), "apex_pct": round(float(a) / L, 4) if a is not None and L else None})
    return out, L


def _review(h, cfg, me, ss, idx):
    """The same review and corner coach the session page shows (both come from the page's cache)."""
    from .server import _same_combo, practice_key
    si = h._module("sessionibt")
    earlier = [{k: v for k, v in x.items() if k != "paths"} for x in ss
               if _same_combo(x, me["car_path"], me["track_code"]) and x["sort"] < me["sort"]]
    hist = [[x.get("id"), x.get("best"), x.get("median")] for x in earlier]
    key = practice_key("review", me.get("paths") or [], hist)
    rv = h.server.practice.get(cfg["cache_dir"], key,
                               lambda: si.review(me.get("paths") or [], history=earlier, idx=idx))
    cards = h.server.corner_coach_for(cfg, ss, me["car_path"], me["track_code"], upto=me["sort"])
    return rv, cards


def live_now(h) -> bool:
    """True while the coach is running or the recorder is in an iRacing session: no Siyanqaba call then (rule 3)."""
    if getattr(h.server, "coach", None) is not None:
        return True
    try:
        from .. import liverec
        st = liverec.status_view()
        return bool(st.get("running") and (st.get("session") or st.get("state") == "recording"))
    except Exception:  # noqa: BLE001
        return False


def _sheet(h, cfg, me, ss, idx):
    _a, c1, factsheet, _f, _z = _mods()
    rv, cards = _review(h, cfg, me, ss, idx)
    sid = c1.session_id(me.get("paths") or [])
    units = str(cfg.get("units") or "mph")
    # fuel in litres, as the session page shows it (sessionibt's fuel text)
    return factsheet.build(rv, cards, session_id=sid, speed_unit=units, fuel_unit="L",
                           feel=_store(cfg).load(sid)), cards, sid


def _zerver_state(cfg, me) -> dict:
    _a, _c, _fs, _f, zp = _mods()
    dest = str(cfg.get("night_shift_folder") or "")
    if not dest:
        return {"set": False, "sent": False, "text": "Off. Pick the Zerver's night shift folder in Settings > Siyanqaba."}
    sent = zp.is_sent(sent_file(cfg), me.get("paths") or [])
    with _auto_lock:
        last = _auto.get("last") or {}
    err = last.get("errors", {}).get(me.get("id")) if isinstance(last.get("errors"), dict) else None
    if sent:
        return {"set": True, "sent": True, "text": "Sent to the Zerver's night shift folder."}
    return {"set": True, "sent": False, "error": bool(err),
            "text": err or "Sessions from the last week go to the Zerver when Debrief opens, or send it now."}


def get(h, r, q):
    aisummary, c1, factsheet, feel, _zp = _mods()
    cfg, me, ss, idx = _session(h, q)
    if r == "api/siyanqaba/session":
        fs, cards, sid = _sheet(h, cfg, me, ss, idx)
        corners, L = _corners(cards)
        d = _store(cfg).load(sid)
        return h._json({"ok": True, "session_id": sid, "corners": corners, "length_m": L or None,
                        "tags": [{"id": t, "label": feel.LABELS[t]} for t in feel.TAGS],
                        "phases": [{"id": p, "label": feel.LABELS[p]} for p in feel.PHASES],
                        "feel": {"taps": d["taps"], "lines": feel.summary(d)},
                        "zerver": _zerver_state(cfg, me),
                        "summary": {"text": aisummary.plain(fs["clauses"], fs["required"]), "source": "plain"},
                        "not_proven_yet": fs["not_proven_yet"],
                        "ai_set": bool(cfg.get("siyanqaba_address")), "live": live_now(h)})
    # api/siyanqaba/summary
    fs, _cards, _sid = _sheet(h, cfg, me, ss, idx)
    if live_now(h):
        return h._json({"ok": True, "text": aisummary.plain(fs["clauses"], fs["required"]), "source": "plain",
                        "problems": ["You're in an iRacing session: Siyanqaba waits until you're done."]})
    got = aisummary.ordered(cfg.get("siyanqaba_address") or "", fs["clauses"], fs["required"])
    if got["problems"]:
        app.say("Siyanqaba summary: plain order (" + "; ".join(got["problems"]) + ")")
    return h._json({"ok": True, **got})


def post(h, r, body):
    _a, c1, _fs, feel, zp = _mods()
    b = body if isinstance(body, dict) else {}
    cfg, me, ss, idx = _session(h, {"id": b.get("id")})
    sid = c1.session_id(me.get("paths") or [])
    store = _store(cfg)
    if r == "api/zerver/push":
        out = zp.push(cfg.get("night_shift_folder") or "", sid, me.get("paths") or [], feel_file=store.path(sid),
                      sent_file=sent_file(cfg), info={"car_path": me.get("car_path"), "track_key": me.get("track_code")})
        if out["error"]:
            raise ValueError(out["error"])
        return h._json({"ok": True, "sent": out["sent"], "zerver": _zerver_state(cfg, me)})
    if r == "api/feel":
        _rv, cards = _review(h, cfg, me, ss, idx)
        corners, L = _corners(cards)
        cid = str(b.get("corner") or "")
        known = next((c for c in corners if c["id"] == cid), None)
        if known is None:
            raise ValueError("Pick a corner from this session first.")
        d = store.add(sid, {"track_key": me.get("track_code"), "car_path": me.get("car_path"), "length_m": L},
                      cid, known["apex_pct"], b.get("tag"), b.get("phase") or None)
    else:
        idx_ = b.get("index")
        d = store.undo(sid, None if idx_ is None else int(idx_))
    if cfg.get("night_shift_folder") and zp.is_sent(sent_file(cfg), me.get("paths") or []):
        zp.copy_feel(cfg["night_shift_folder"], sid, store.path(sid))   # keep the Zerver's copy up to date
    return h._json({"ok": True, "feel": {"taps": d["taps"], "lines": feel.summary(d)}})


def auto_push(server, cfg, sessions) -> bool:
    """Send last week's unsent sessions in the background (one run at a time). False when there's nothing to do."""
    dest = str(cfg.get("night_shift_folder") or "")
    if not dest or not sessions:
        return False
    with _auto_lock:
        if _auto["busy"]:
            return False
        _auto["busy"] = True

    def run():
        errors, sent = {}, 0
        try:
            from .. import c1, zerverpush as zp
            store = _store(cfg)
            for s in zp.pending(sessions, sent_file(cfg)):
                paths = s.get("paths") or []
                if not zp.settled(paths):
                    continue
                sid = c1.session_id(paths)
                out = zp.push(dest, sid, paths, feel_file=store.path(sid), sent_file=sent_file(cfg),
                              info={"car_path": s.get("car_path"), "track_key": s.get("track_code")})
                if out["error"]:
                    errors[s.get("id")] = out["error"]
                    if "can't reach" in out["error"]:
                        break                                   # the Zerver is off: try again next time
                else:
                    sent += 1
            if sent or errors:
                app.say(f"Night shift folder: {sent} session(s) sent" + (f", {len(errors)} not sent" if errors else ""))
        except Exception:  # noqa: BLE001
            app.say("ERROR sending to the night shift folder:\n" + traceback.format_exc())
        finally:
            with _auto_lock:
                _auto["busy"] = False
                _auto["last"] = {"errors": errors, "sent": sent}

    threading.Thread(target=run, name="zerver-push", daemon=True).start()
    return True
