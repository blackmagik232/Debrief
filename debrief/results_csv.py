"""Read the results file iRacing's results page exports (2.7.5), so a race result doesn't have to be typed.

parse(bytes or text) finds the results table by its column names (lines above it are the race's details), keeps the
race session's rows and gives each driver the fields results.validate takes. In a multi-class race the positions and
the field size are within the driver's own class, as on the official page. A value the file doesn't have, or that
makes no sense, is simply left out, so the person can still type it. Nothing is saved here.
"""
from __future__ import annotations

import csv
import io
import re

MAX_DRIVERS = 200


class ResultsFileError(ValueError):
    """The file isn't an iRacing results table, told in plain words."""


def clean_name(name) -> str:
    """A name without the digits iRacing adds to tell same-named members apart ('Gustavo Santos8')."""
    return re.sub(r"(?<=[^\W\d])\d+$", "", str(name or "").strip()).strip()


def same_name(a, b) -> bool:
    return " ".join(clean_name(a).lower().split()) == " ".join(clean_name(b).lower().split())


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _text(data) -> str:
    if isinstance(data, str):
        return data.lstrip("﻿")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ResultsFileError("That file isn't a text CSV. Download the results CSV from the iRacing results page.")


def _sr(v):
    n = _int(v)
    if n is None or n < 0:
        return None
    sr = round(n / 100.0, 2)        # iRacing writes 399 for 3.99
    return sr if sr <= 4.99 else None


def parse(data) -> dict:
    """{"drivers": [{"name", "car", "class", "fields": {...}}], "multiclass": bool}."""
    try:
        rows = list(csv.reader(io.StringIO(_text(data))))
    except csv.Error:
        raise ResultsFileError("That file couldn't be read as a CSV.") from None
    head = next((k for k, r in enumerate(rows) if "Fin Pos" in r and "Name" in r), None)
    if head is None:
        raise ResultsFileError("That file has no results table. Use the CSV from the iRacing results page.")
    cols = {name.strip(): k for k, name in enumerate(rows[head])}

    def get(r, name):
        k = cols.get(name)
        return r[k].strip() if k is not None and k < len(r) else ""

    body = [r for r in rows[head + 1:] if len(r) > 1 and _int(get(r, "Fin Pos")) is not None and get(r, "Name")]
    if "Session Name" in cols:
        race = [r for r in body if get(r, "Session Name").upper() == "RACE"]
        body = race or body
    if not body:
        raise ResultsFileError("The results table in that file is empty.")
    body = body[:MAX_DRIVERS]
    class_of = lambda r: get(r, "Car Class ID") or get(r, "Car Class") or "all"  # noqa: E731
    groups = {}
    for r in body:
        groups.setdefault(class_of(r), []).append(r)
    drivers = []
    for r in sorted(body, key=lambda x: _int(get(x, "Fin Pos"))):
        group = groups[class_of(r)]
        finish = sorted(group, key=lambda x: _int(get(x, "Fin Pos")))
        starters = [x for x in group if _int(get(x, "Start Pos")) is not None]
        fields = {"finish_pos": next(i for i, x in enumerate(finish, 1) if x is r), "field_size": len(group)}
        if r in starters:
            fields["start_pos"] = next(i for i, x in enumerate(sorted(starters, key=lambda x: _int(get(x, "Start Pos"))), 1)
                                       if x is r)
        for key, col in (("incidents", "Inc"), ("laps_led", "Laps Led")):
            v = _int(get(r, col))
            if v is not None and v >= 0:
                fields[key] = v
        for key, col in (("irating_before", "Old iRating"), ("irating_after", "New iRating")):
            v = _int(get(r, col))
            if v is not None and v > 0:
                fields[key] = v
        for key, col in (("sr_before", "Old License Sub-Level"), ("sr_after", "New License Sub-Level")):
            v = _sr(get(r, col))
            if v is not None:
                fields[key] = v
        out = get(r, "Out").lower()
        if out in ("running", "disconnected"):
            fields["finish_status"] = out
        drivers.append({"name": clean_name(get(r, "Name")), "car": get(r, "Car #").lstrip("#"),
                        "class": get(r, "Car Class"), "fields": fields})
    return {"drivers": drivers, "multiclass": len(groups) > 1}


def pick(parsed: dict, name) -> dict | None:
    """The driver with this name, or None (the window then lets the person pick)."""
    hits = [d for d in parsed.get("drivers") or [] if same_name(d["name"], name)]
    return hits[0] if len(hits) == 1 else None
