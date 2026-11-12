"""Debrief code map: every module, what it imports, who imports it, which tests cover it, and which window API
routes it answers. Writes a JSON graph (for the constellation page) and a Markdown index (for reading back).

    python tools/codemap.py [--json out.json] [--md out.md]

Read only; no network. Re-run after every release so the map stays true.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "debrief"

# area of the app each module belongs to (the constellation's colours); anything not listed is "core"
AREAS = {
    "analysis": "engine analysis_plus race_analysis insights depth technique_plus skills profile goals incidentmap "
                "startreport racecraft rival_read replay_scan markers session_review carcompare leaderboards results "
                "results_csv cornercoach",
    "prep": "prep prepare intel strategy practiceplan today replan endurance endureview schedule series",
    "car": "setuplab setupadvice setupshare balance handling pressurelearn",
    "live": "livecoach livedata liverec livetray coachvoice voice voicepack audioin audioout cuelog overlay perfmeter "
            "crewchief raceroom endurancelog wheelmute noterec notes recordings iracing cornernotes ibt sessionibt",
    "team": "team teamref teamsync members cards discord",
    "core": "app g61 sync library fsutil names units xlsx brand credits whatsnew updater ed25519 report firstrun "
            "racecap_link",
}
AREA_OF = {m: a for a, ms in AREAS.items() for m in ms.split()}
AREA_TEXT = {
    "core": "Settings, Garage 61 client, history sync, the review library, the updater and shared helpers",
    "analysis": "Turning laps into findings: corners, consistency, starts, incidents, skills, car vs car",
    "prep": "Before you drive: Prepare, Today, practice plans, strategy, stint plans, schedules",
    "car": "Setup Lab: setups from .ibt files, handling, pressures, one change at a time",
    "live": "While you drive: reading iRacing, recording, the coach, voice, overlay, crew chief, Race Room",
    "team": "Garage 61 teams, teammates' laps, leaderboards, share cards, Discord",
    "desktop": "The window's local server and its page handlers (debrief/desktop)",
    "ui": "The window itself: index.html, app.js, help.js, raceroom.js, app.css, and the review/library templates",
    "tests": "tests/: one file per release or feature",
}


def _lines(p: Path) -> int:
    try:
        return sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def _mod_id(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _resolve(cur: Path, level: int, module: str | None, name: str | None) -> list[str]:
    """Paths of the internal modules a relative import names."""
    base = cur.parent
    for _ in range(level - 1):
        base = base.parent
    out = []
    if module:
        tgt = base.joinpath(*module.split("."))
        if tgt.with_suffix(".py").is_file():
            out.append(tgt.with_suffix(".py"))
        elif (tgt / "__init__.py").is_file():
            if name and (tgt / f"{name}.py").is_file():
                out.append(tgt / f"{name}.py")
            else:
                out.append(tgt / "__init__.py")
    elif name:
        if (base / f"{name}.py").is_file():
            out.append(base / f"{name}.py")
        elif (base / name / "__init__.py").is_file():
            out.append(base / name / "__init__.py")
    return out


def python_modules() -> dict:
    mods = {}
    for p in sorted(PKG.rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        doc = (ast.get_docstring(tree) or "").strip().split("\n\n")[0].replace("\n", " ")
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                for a in node.names:
                    for t in _resolve(p, node.level, node.module, a.name):
                        imports.add(_mod_id(t))
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("debrief"):
                parts = node.module.split(".")[1:]
                for a in node.names:
                    for t in _resolve(PKG / "x.py", 1, ".".join(parts) or None, a.name):
                        imports.add(_mod_id(t))
        funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        methods = [f"{c.name}.{m.name}" for c in tree.body if isinstance(c, ast.ClassDef)
                   for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        mid = _mod_id(p)
        stem = p.stem
        area = "desktop" if "desktop" in p.parts else AREA_OF.get(stem, "core")
        imports.discard(mid)
        mods[mid] = {"id": mid, "kind": "py", "area": area, "lines": _lines(p), "doc": doc[:400],
                     "functions": funcs, "classes": classes, "methods": methods, "imports": sorted(imports),
                     "public": [f for f in funcs if not f.startswith("_")][:40]}
    return mods


def routes(mods: dict) -> dict:
    """{"api/x": {"handler": module id, "method": "GET"|"POST", "callers": [ui files]}}"""
    out = {}
    srv = (PKG / "desktop" / "server.py").read_text(encoding="utf-8")
    for m in re.finditer(r'if r == "(api/[a-z0-9/_-]+)"', srv):
        out.setdefault(m.group(1), {"handler": "debrief/desktop/server.py", "method": "POST"})
    # routes listed together: `if r in ("api/a", "api/b", ...)` (GET list near do_GET, POST list near do_POST)
    post_at = srv.find("def do_POST")
    for m in re.finditer(r"if r in \(([^)]*)\)", srv):
        meth = "POST" if post_at != -1 and m.start() > post_at else "GET"
        for r in re.findall(r'"(api/[a-z0-9/_-]+)"', m.group(1)):
            out.setdefault(r, {"handler": "debrief/desktop/server.py", "method": meth})
    for m in re.finditer(r"def _get_([a-z0-9_]+)\(", srv):
        out.setdefault("api/" + m.group(1).replace("_", "/"), {"handler": "debrief/desktop/server.py", "method": "GET"})
    for p in sorted((PKG / "desktop").glob("*.py")):
        s = p.read_text(encoding="utf-8")
        for kind in ("GET", "POST"):
            m = re.search(kind + r'_ROUTES\s*=\s*\(([^)]*)\)', s, re.S)
            if m:
                for r in re.findall(r'"(api/[^"]+)"', m.group(1)):
                    out[r] = {"handler": _mod_id(p), "method": kind}
    ui = PKG / "desktop" / "ui"
    for f in sorted(ui.glob("*.js")):
        s = f.read_text(encoding="utf-8")
        for m in re.finditer(r"""\b(?:api|post)\(\s*['"]([a-z0-9/_-]+)""", s):
            r = "api/" + m.group(1)
            rec = out.get(r)
            if rec is None:   # a GET with a query, or a path the server builds: match the longest known prefix
                cand = [k for k in out if r.startswith(k)]
                rec = out[max(cand, key=len)] if cand else out.setdefault(r, {"handler": "debrief/desktop/server.py", "method": "?"})
            rec.setdefault("callers", set()).add(_mod_id(f))
    for v in out.values():
        v["callers"] = sorted(v.get("callers", ()))
    return dict(sorted(out.items()))


def ui_files() -> dict:
    out = {}
    for f in sorted((PKG / "desktop" / "ui").glob("*.*")) + sorted((PKG / "templates").glob("*.html")):
        if f.suffix not in (".js", ".html", ".css"):
            continue
        s = f.read_text(encoding="utf-8", errors="replace")
        fns = re.findall(r"(?:^|[;\s])(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", s)
        out[_mod_id(f)] = {"id": _mod_id(f), "kind": f.suffix[1:], "area": "ui", "lines": _lines(f),
                           "doc": "", "functions": fns[:400], "classes": [], "imports": [], "public": fns[:40]}
    return out


def tests(mods: dict) -> dict:
    out = {}
    for t in sorted((ROOT / "tests").glob("test_*.py")):
        s = t.read_text(encoding="utf-8", errors="replace")
        hits = set()
        for m in re.finditer(r"from debrief(?:\.(\w+))? import ([\w ,()]+)", s):
            sub, names = m.group(1), [n.strip(" ()").split(" as ")[0].strip() for n in m.group(2).split(",")]
            for n in names:
                for cand in ([f"debrief/{sub}/{n}.py"] if sub else []) + [f"debrief/{n}.py", f"debrief/{sub}.py" if sub else ""]:
                    if cand in mods:
                        hits.add(cand)
                        break
        for m in re.finditer(r"import debrief\.(\w+)(?:\.(\w+))?", s):
            cand = f"debrief/{m.group(1)}/{m.group(2)}.py" if m.group(2) else f"debrief/{m.group(1)}.py"
            if cand in mods:
                hits.add(cand)
        if "ui/app.js" in s or 'UI / "app.js"' in s or "playwright" in s:
            hits.add("debrief/desktop/ui/app.js")
        doc = s.split('"""')[1].strip().split("\n")[0] if s.startswith('"""') else ""
        out[_mod_id(t)] = {"id": _mod_id(t), "kind": "test", "area": "tests", "lines": _lines(t), "doc": doc[:300],
                           "covers": sorted(hits)}
    return out


# ---------------------------------------------------------------- 2.33: the window, element by element
GENERIC = set("""btn row note panel item pill msg ok bad active hidden busy done warn on off sub lede wide small big ghost primary
 check field mono path tick link linkbtn chip tag card list grid head body wrap open close show hide icon dot bar box seg
 title text name label value empty error good fail flash quiet frame view""".split())
FN_RE = re.compile(r"(?:^|[;{}\n])\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|(?:^|[;{}\n])\s*const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
ID_RE = re.compile(r"""\$\(\s*['"]([\w-]+)['"]\s*\)|getElementById\(\s*['"]([\w-]+)['"]\s*\)|['"(\s,>]#([A-Za-z][\w-]+)""")
NAV_RE = re.compile(r"""data-nav=?\\?['"]?([a-z]+)|dataset\.nav\s*===?\s*['"]([a-z]+)|goNav\(\s*['"]([a-z]+)""")
CLS_RE = re.compile(r"""['"\s(>,]\.([a-z][\w-]{2,})""")


def _js_functions(path: Path) -> dict:
    """{name: {"file", "line", "touches": {"ids", "navs", "classes"}}} for the top-level functions of a window script."""
    src = path.read_text(encoding="utf-8", errors="replace")
    marks = [(m.start(), m.group(1) or m.group(2)) for m in FN_RE.finditer(src)]
    out = {}
    for k, (pos, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(src)
        body = src[pos:end]
        ids = {a or b or c for a, b, c in ID_RE.findall(body)}
        navs = {a or b or c for a, b, c in NAV_RE.findall(body)}
        cls = {c for c in CLS_RE.findall(body) if c not in GENERIC}
        if name in out:            # a second definition with the same name: keep both bodies' touches
            out[name]["touches"]["ids"] |= ids
            out[name]["touches"]["navs"] |= navs
            out[name]["touches"]["classes"] |= cls
            continue
        out[name] = {"file": _mod_id(path), "line": src.count("\n", 0, pos) + 1,
                     "touches": {"ids": ids, "navs": navs, "classes": cls}}
    return out


def _ui_names() -> tuple:
    """(element ids, class names, sidebar pages) that exist in the window's HTML, scripts and stylesheet."""
    ui = PKG / "desktop" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = "".join(f.read_text(encoding="utf-8") for f in ui.glob("*.js"))
    css = (ui / "app.css").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([\w-]+)"', html)) | {a or b for a, b, _c in ID_RE.findall(js) if a or b}
    navs = set(re.findall(r'data-nav="([a-z]+)"', html))
    classes = (set(re.findall(r"\.([a-z][\w-]{2,})", css)) | set(re.findall(r'class="([^"]+)"', html) and
               " ".join(re.findall(r'class="([^"]+)"', html)).split())) - GENERIC
    return {i for i in ids if len(i) >= 3}, {c for c in classes if not re.match(r"^\d", c)}, navs


def window_map(test_texts: dict) -> dict:
    """Which tests touch each window element, sidebar page and function, so a change can name its tests:
    {"elements": {id: [tests]}, "navs": {page: [tests]}, "classes": {class: [tests]},
     "functions": {name: {"file", "line", "touches", "tests" (direct), "via" (tests through what it touches)}}}"""
    ids, classes, navs = _ui_names()
    el, nv, cl = defaultdict(set), defaultdict(set), defaultdict(set)
    fn_names = {}
    for f in sorted((PKG / "desktop" / "ui").glob("*.js")):
        fn_names.update(_js_functions(f))
    for tid, txt in test_texts.items():
        if "desktop" not in txt and "ui" not in txt and "playwright" not in txt and "app.js" not in txt:
            continue
        for a, b, c in ID_RE.findall(txt):
            i = a or b or c
            if i in ids:
                el[i].add(tid)
        for i in ids:                       # Python tests say pg.click("#x"), pg.text_content("#x"), '"x"' in JS
            if ("#" + i) in txt or f"getElementById('{i}')" in txt or f'getElementById("{i}")' in txt:
                el[i].add(tid)
        for a, b, c in NAV_RE.findall(txt):
            n = a or b or c
            if n in navs:
                nv[n].add(tid)
        for c in CLS_RE.findall(txt):
            if c in classes:
                cl[c].add(tid)
        for name in fn_names:
            if len(name) >= 5 and re.search(r"\b" + re.escape(name) + r"\b", txt):
                fn_names[name].setdefault("_direct", set()).add(tid)
    functions = {}
    for name, f in sorted(fn_names.items()):
        t = f["touches"]
        via = set()
        for i in t["ids"]:
            via |= el.get(i, set())
        for n in t["navs"]:
            via |= nv.get(n, set())
        for c in t["classes"]:
            via |= cl.get(c, set())
        direct = f.get("_direct", set())
        functions[name] = {"file": f["file"], "line": f["line"],
                           "touches": {k: sorted(v) for k, v in t.items()},
                           "tests": sorted(direct), "via": sorted(via - direct)}
    return {"elements": {k: sorted(v) for k, v in sorted(el.items())}, "navs": {k: sorted(v) for k, v in sorted(nv.items())},
            "classes": {k: sorted(v) for k, v in sorted(cl.items())}, "functions": functions}


def which(g: dict, q: str) -> dict:
    """Tests to run when changing q: a module path or file name, a Python or window function, an element id (#x),
    a class (.x), a sidebar page (nav:x) or a route (api/x). {"what", "tests", "why"}"""
    q = q.strip()
    W, N = g["window"], g["nodes"]
    hits, why = set(), []
    qq = q.lstrip("#.")
    if q.startswith("api/") and q in g["routes"]:
        h = g["routes"][q]["handler"]
        hits |= set(N[h]["tests"]) if h in N else set()
        why.append(f"route {q} is answered by {h}")
    if q.startswith("nav:") or qq in W["navs"]:
        n = q[4:] if q.startswith("nav:") else qq
        hits |= set(W["navs"].get(n, []))
        why.append(f"sidebar page {n}")
    if q.startswith("#") or (not q.startswith(".") and qq in W["elements"]):
        hits |= set(W["elements"].get(qq, []))
        why.append(f"element #{qq}")
    if q.startswith(".") or qq in W["classes"]:
        hits |= set(W["classes"].get(qq, []))
        why.append(f"class .{qq}")
    if q in W["functions"]:
        f = W["functions"][q]
        hits |= set(f["tests"]) | set(f["via"])
        why.append(f"window function {q} ({f['file']}:{f['line']}) touches " +
                   ", ".join(f"#{i}" for i in f["touches"]["ids"][:8]) + (" ..." if len(f["touches"]["ids"]) > 8 else ""))
    mods = [k for k in N if k == q or k.endswith("/" + q) or k.endswith("/" + q + ".py")]
    for k, n in N.items():
        if n["kind"] == "py" and (q in (n.get("functions") or []) or
                                  any(m == q or m.endswith("." + q) for m in n.get("methods") or [])):
            mods.append(k)
    for m in dict.fromkeys(mods):
        hits |= set(N[m]["tests"])
        imp = [i for i in N[m]["imported_by"]]
        for i in imp:                        # one step out: what uses it directly
            hits |= set(N[i]["tests"])
        why.append(f"module {m} and the {len(imp)} modules that import it")
    return {"what": q, "tests": sorted(hits), "why": why}


def build() -> dict:
    mods = python_modules()
    ui = ui_files()
    rts = routes(mods)
    tst = tests({**mods, **ui})
    texts = {_mod_id(t): t.read_text(encoding="utf-8", errors="replace") for t in sorted((ROOT / "tests").glob("test_*.py"))}
    win = window_map(texts)
    nodes = {**mods, **ui}
    for n in nodes.values():
        n["imported_by"], n["tests"], n["routes"] = [], [], []
    for n in mods.values():
        for i in n["imports"]:
            if i in nodes:
                nodes[i]["imported_by"].append(n["id"])
    for t in tst.values():
        for c in t["covers"]:
            if c in nodes:
                nodes[c]["tests"].append(t["id"])
    for r, v in rts.items():
        if v["handler"] in nodes:
            nodes[v["handler"]]["routes"].append(r)
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h %s"], capture_output=True, text=True,
                              encoding="utf-8", timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        head = ""
    from importlib import util
    spec = util.spec_from_file_location("v", PKG / "__init__.py")
    ver = ""
    try:
        m = util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ver = getattr(m, "__version__", "")
    except Exception:  # noqa: BLE001
        pass
    return {"version": ver, "commit": head, "areas": AREA_TEXT, "nodes": nodes, "tests": tst, "routes": rts, "window": win,
            "totals": {"modules": len(mods), "ui_files": len(ui), "tests": len(tst), "routes": len(rts),
                       "lines": sum(n["lines"] for n in nodes.values()),
                       "test_lines": sum(t["lines"] for t in tst.values())}}


def markdown(g: dict) -> str:
    N = g["nodes"]
    L = [f"# Debrief code map (v{g['version']}, {g['commit']})", "",
         "Generated by `tools/codemap.py`. Read this before changing Debrief: find the module, what it touches, and "
         "the tests to run. Regenerate after every release.", "",
         f"Totals: {g['totals']['modules']} Python modules, {g['totals']['ui_files']} window/template files, "
         f"{g['totals']['tests']} test files, {g['totals']['routes']} window API routes, {g['totals']['lines']:,} app lines.", "",
         "## Areas", ""]
    for a, t in g["areas"].items():
        L.append(f"- **{a}**: {t}")
    L += ["", "## Hotspots (most imported, then biggest)", ""]
    hot = sorted(N.values(), key=lambda n: (-len(n["imported_by"]), -n["lines"]))[:15]
    for n in hot:
        L.append(f"- `{n['id']}`: imported by {len(n['imported_by'])}, {n['lines']} lines, {len(n['tests'])} test files")
    L += ["", "## Modules by area", ""]
    for a in g["areas"]:
        ms = sorted((n for n in N.values() if n["area"] == a), key=lambda n: n["id"])
        if not ms:
            continue
        L += [f"### {a}", ""]
        for n in ms:
            L.append(f"**`{n['id']}`** ({n['lines']} lines){': ' + n['doc'][:220] if n['doc'] else ''}")
            if n["imports"]:
                L.append("  - uses: " + ", ".join(i.split("/")[-1] for i in n["imports"]))
            if n["imported_by"]:
                L.append("  - used by: " + ", ".join(i.split("/")[-1] for i in sorted(n["imported_by"])))
            if n["routes"]:
                L.append("  - answers: " + ", ".join(n["routes"][:25]) + (" ..." if len(n["routes"]) > 25 else ""))
            if n["tests"]:
                L.append("  - tests: " + " ".join(t.split("/")[-1] for t in n["tests"][:14]) +
                         (f" (+{len(n['tests']) - 14})" if len(n["tests"]) > 14 else ""))
            if n["public"] and n["kind"] == "py":
                L.append("  - key functions: " + ", ".join(n["public"][:14]))
        L.append("")
    W = g["window"]
    L += ["## Window: which tests use each page and element", "",
          "Ask the generator instead of reading this: `python tools/codemap.py --which <thing>` (a module, a function, "
          "#element, .class, nav:page or api/route) prints the test files to run.", "",
          "Sidebar pages: " + "; ".join(f"`{k}` {len(v)}" for k, v in W["navs"].items()), "",
          "Most-tested elements: " + ", ".join(f"`#{k}` ({len(v)})" for k, v in
                                               sorted(W["elements"].items(), key=lambda kv: -len(kv[1]))[:30]), "",
          f"Window functions mapped: {len(W['functions'])} (each with the elements it touches and the tests that use them).", ""]
    L += ["## Window API routes", "", "| Route | Method | Handler | Called from |", "| --- | --- | --- | --- |"]
    for r, v in g["routes"].items():
        L.append(f"| `{r}` | {v['method']} | {v['handler'].split('/')[-1]} | {', '.join(c.split('/')[-1] for c in v['callers']) or '-'} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(ROOT / "codemap.json"))
    ap.add_argument("--md", default=str(ROOT / "CODEMAP.md"))
    ap.add_argument("--which", action="append", help="print the tests to run when changing this (repeatable)")
    a = ap.parse_args()
    g = build()
    if a.which:
        allt = set()
        for q in a.which:
            r = which(g, q)
            allt |= set(r["tests"])
            print(f"{q}: {len(r['tests'])} test files  ({'; '.join(r['why']) or 'nothing by that name'})")
        print("python3 tests/run_all.py " + " ".join(sorted(allt)))
        raise SystemExit(0)
    Path(a.json).write_text(json.dumps(g, indent=1), encoding="utf-8")
    Path(a.md).write_text(markdown(g), encoding="utf-8")
    print(f"{g['totals']['modules']} modules, {g['totals']['ui_files']} UI files, {g['totals']['tests']} tests, "
          f"{g['totals']['routes']} routes -> {a.json}, {a.md}")
