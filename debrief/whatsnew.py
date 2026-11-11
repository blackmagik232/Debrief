"""2.30.4 What's new: every version tells its users what changed.

The text comes from the top of CHANGELOG.md, the same entry the GitHub release shows. An entry looks like:

    ## 2.30.4 "Name"

    Plain words for the person using Debrief (paragraphs and "- " bullets, **bold** and `code` allowed).

    ### For the record
    Developer notes: files, tests, "Not proven yet". Shown on GitHub, never in the app's What's new.

Debrief shows What's new once after it updates to a new version (not on a brand-new install, which has its own
setup checklist), and any time from Help > What's new.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

RECORD_HEADING = "### For the record"
_HEAD = re.compile(r'^## (\d+\.\d+\.\d+)(?:\s+"([^"]*)")?\s*$')
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def changelog_path() -> Path:
    from . import app
    for base in (app.RES, app.HERE, Path(__file__).resolve().parent.parent):
        p = Path(base) / "CHANGELOG.md"
        if p.is_file():
            return p
    return Path(app.RES) / "CHANGELOG.md"


def entries(text: str) -> list[dict]:
    """Every version entry, newest first: {version, name, user, record} (user and record are raw markdown)."""
    out, cur, lines = [], None, []

    def close():
        if cur is None:
            return
        body = "\n".join(lines).strip("\n")
        user, _, record = body.partition(RECORD_HEADING)
        cur["user"], cur["record"] = user.strip(), record.strip()
        out.append(cur)

    for raw in text.replace("\r\n", "\n").split("\n"):
        m = _HEAD.match(raw)
        if m:
            close()
            cur, lines = {"version": m.group(1), "name": m.group(2) or ""}, []
        elif cur is not None:
            lines.append(raw)
    close()
    return out


def entry_for(version: str, text: str | None = None) -> dict | None:
    if text is None:
        try:
            text = changelog_path().read_text(encoding="utf-8")
        except OSError:
            return None
    for e in entries(text):
        if e["version"] == version:
            return e
    return None


def _parts(s: str) -> list[list]:
    """'a **b** `c`' -> [["a ", ""], ["b", "b"], ["c", "code"]]: the page builds text nodes, never HTML."""
    out = []
    for piece in _INLINE.split(s):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            out.append([piece[2:-2], "b"])
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append([piece[1:-1], "code"])
        else:
            out.append([piece, ""])
    return out


def blocks(markdown: str) -> list[dict]:
    """The user part as safe blocks: {"kind": "p"|"li", "depth": 0|1, "parts": [...]}. Wrapped lines join up."""
    out: list[dict] = []
    for raw in markdown.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out.append({"kind": "gap"})
            continue
        m = re.match(r"^(\s*)[-*] (.*)$", line)
        if m:
            out.append({"kind": "li", "depth": 1 if len(m.group(1)) >= 2 else 0, "text": m.group(2).strip()})
        elif out and out[-1].get("kind") in ("li", "p"):
            out[-1]["text"] += " " + line.strip()      # a wrapped line belongs to the one above
        else:
            out.append({"kind": "p", "depth": 0, "text": line.strip().lstrip("#").strip()})
    return [{"kind": b["kind"], "depth": b["depth"], "parts": _parts(b["text"])} for b in out if b["kind"] != "gap"]


def problems(text: str, version: str) -> list[str]:
    """Why a release would go out without a proper What's new (empty list = fine). Used by the tests and CI."""
    es = entries(text)
    if not es:
        return ["CHANGELOG.md has no '## x.y.z' entries."]
    top = es[0]
    errs = []
    if top["version"] != version:
        errs.append(f"The newest CHANGELOG entry is {top['version']}, but debrief/__init__.py says {version}. "
                    "Add a What's new entry for this version at the top.")
    if len(top["user"]) < 40:
        errs.append(f"The {top['version']} entry has no What's new text for users (the part above "
                    f"'{RECORD_HEADING}').")
    if re.search(r"`[^`]+\.py`", top["user"]):
        errs.append(f"The {top['version']} What's new names code files; move those to '{RECORD_HEADING}'.")
    return errs


def release_notes(text: str, version: str) -> str:
    """The GitHub release body: What's new, then the record."""
    e = entry_for(version, text)
    if not e:
        return ""
    title = f"## What's new in Debrief {version}" + (f" \"{e['name']}\"" if e["name"] else "")
    body = [title, "", e["user"]]
    if e["record"]:
        body += ["", RECORD_HEADING, "", e["record"]]
    return "\n".join(body).strip() + "\n"


# ------------------------------------------------------------------ shown once per version
def _seen_path() -> Path:
    from . import app
    return Path(app.HERE) / "whatsnew.json"


def _read_seen() -> str | None:
    try:
        d = json.loads(_seen_path().read_text(encoding="utf-8"))
        return str(d.get("seen")) if isinstance(d, dict) and d.get("seen") else None
    except (OSError, ValueError):
        return None


def mark_seen(version: str) -> None:
    try:
        p = _seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"seen": version}), encoding="utf-8")
    except OSError:
        pass


def should_show(version: str, has_setup: bool) -> bool:
    """True once after an update. A brand-new install (no Garage 61 token yet) records the version silently."""
    seen = _read_seen()
    if seen == version:
        return False
    if seen is None and not has_setup:
        mark_seen(version)
        return False
    return True


def payload(version: str, auto: bool, has_setup: bool) -> dict:
    e = entry_for(version)
    show = should_show(version, has_setup) if auto else True
    if auto and show:
        mark_seen(version)        # shown now; never again for this version
    if not e or not e["user"]:
        return {"ok": True, "show": False, "version": version}
    return {"ok": True, "show": show, "version": version, "name": e["name"], "blocks": blocks(e["user"])}


if __name__ == "__main__":     # CI: python -m debrief.whatsnew notes|check
    import sys
    from . import __version__
    txt = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    if sys.argv[1:] == ["check"]:
        errs = problems(txt, __version__)
        print("\n".join(errs) or f"What's new for {__version__} is ready.")
        sys.exit(1 if errs else 0)
    sys.stdout.write(release_notes(txt, __version__))
