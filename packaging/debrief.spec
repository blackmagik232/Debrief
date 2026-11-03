# PyInstaller spec for the installed Debrief app (one folder, no console window).
#
# Build from the repository root:
#     pip install -r requirements.txt pyinstaller
#     pyinstaller packaging/debrief.spec --noconfirm --clean
# Output: dist/Debrief/Debrief.exe (plus its _internal folder). The Inno Setup script
# packaging/debrief.iss turns that folder into Debrief-Setup-<version>.exe.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # SPECPATH = the folder this spec is in (packaging/)
PKG = ROOT / "debrief"
VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)

# Bundled files keep the package layout, because the code finds them next to its own modules
# (debrief/templates, debrief/desktop/ui, debrief/desktop/icon.png). settings.example.toml goes to the
# bundle root (sys._MEIPASS), where app.RES points when frozen.
datas = [(str(p), "debrief/templates") for p in sorted((PKG / "templates").glob("*.html"))]
datas += [(str(p), "debrief/desktop/ui") for p in sorted((PKG / "desktop" / "ui").iterdir()) if p.is_file()]
# 2.7.0: the bundled fonts (ui/fonts/*.woff2, fonts.css, LICENSE.txt), so no page ever calls a font service
datas += [(str(p), "debrief/desktop/ui/fonts") for p in sorted((PKG / "desktop" / "ui" / "fonts").iterdir()) if p.is_file()]
if not any(d[1] == "debrief/desktop/ui/fonts" and d[0].endswith("fonts.css") for d in datas):
    raise SystemExit("debrief/desktop/ui/fonts/fonts.css is missing: the fonts would be left out of the build")
datas += [(str(p), "debrief/data") for p in sorted((PKG / "data").glob("*.json"))]   # the season schedule that ships with the app
# Coach 2.0: the voice packs (manifest.json + 16-bit WAV clips + preview.wav per pack; coverage.txt stays out)
_packs = [d for d in sorted((PKG / "data" / "voicepacks").glob("*")) if (d / "manifest.json").is_file()]
for _need in ("engineer",):   # George (engineer) is the default
    if not any(d.name == _need for d in _packs):
        raise SystemExit(f"debrief/data/voicepacks/{_need} is missing: that coach voice would be left out")
for _d in _packs:
    datas += [(str(p), f"debrief/data/voicepacks/{_d.name}") for p in sorted(_d.iterdir())
              if p.is_file() and (p.name == "manifest.json" or p.suffix.lower() == ".wav")]
datas += [(str(PKG / "desktop" / "icon.png"), "debrief/desktop"),
          (str(PKG / "desktop" / "debrief.ico"), "debrief/desktop"),
          (str(ROOT / "settings.example.toml"), ".")]

# app.optional_module() imports feature modules by name (prep, goals, results, team, ...), which
# PyInstaller can't see, so include every debrief module. make_icon is a build-time helper (needs Pillow).
# collect_submodules imports the package in a helper process, which only finds it when the repo root is
# on sys.path. Without this it silently returned nothing, and 2.1.0 shipped without debrief.prep.
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
hiddenimports = collect_submodules("debrief", filter=lambda name: not name.endswith(".make_icon"))
# Belt and braces: every module file in the package is listed by name too, and the build stops if
# collect_submodules missed any of them.
_on_disk = sorted("debrief." + ".".join(p.relative_to(PKG).with_suffix("").parts)
                  for p in PKG.rglob("*.py")
                  if p.name not in ("__init__.py", "__main__.py", "make_icon.py") and "__pycache__" not in p.parts)
_missing = [m for m in _on_disk if m not in hiddenimports]
if _missing:
    print(f"collect_submodules missed {_missing}; adding them by name")
hiddenimports = sorted(set(hiddenimports) | set(_on_disk) | {"irsdk", "yaml", "winsound", "win32com", "win32com.client",
                                                          "win32com.client.dynamic", "pythoncom", "pywintypes"})
for _must in ("debrief.prep", "debrief.goals", "debrief.results", "debrief.team", "debrief.sync",
              "debrief.schedule", "debrief.series", "debrief.notes", "debrief.prepare", "debrief.updater", "debrief.insights",
              "debrief.technique_plus", "debrief.skills", "debrief.session_review", "debrief.profile",
              "debrief.strategy", "debrief.racecraft", "debrief.xlsx", "debrief.intel",
              "debrief.leaderboards", "debrief.discord", "debrief.ed25519",
              "debrief.ibt", "debrief.setuplab", "debrief.livecoach", "debrief.voice",
              "debrief.sessionibt", "debrief.cornercoach",
              "debrief.practiceplan", "debrief.wheelmute", "debrief.firstrun",
              "debrief.balance", "debrief.pressurelearn", "debrief.setupshare", "debrief.teamref",
              "debrief.startreport", "debrief.incidentmap",
              "debrief.voicepack", "debrief.audioout", "debrief.coachvoice", "debrief.cuelog",
              "debrief.desktop.coach2", "debrief.carcompare", "debrief.desktop.carcompare"):
    if _must not in hiddenimports:
        raise SystemExit(f"Build stopped: {_must} would be left out of Debrief.exe")

excludes = ["tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "lib2to3", "pydoc_data",
            "test", "tests", "pytest", "_pytest", "playwright", "PIL", "matplotlib", "IPython",
            "setuptools", "pip", "debrief.desktop.make_icon",
            # optional extras requests/urllib3 can use when present; Debrief doesn't need them
            "cryptography", "OpenSSL", "psutil", "chardet"]

# Version resource on Debrief.exe (File > Properties > Details). Unsigned exes without one are flagged by
# antivirus more often.
_nums = [int(n) for n in re.findall(r"\d+", VERSION)[:3]] + [0, 0, 0, 0]
_vt = tuple(_nums[:4])
VERSION_FILE = ROOT / "build" / "debrief-version.txt"
VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
VERSION_FILE.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_vt}, prodvers={_vt}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('CompanyName', 'Midfield Menace'),
          StringStruct('FileDescription', 'Debrief'),
          StringStruct('FileVersion', '{VERSION}'),
          StringStruct('InternalName', 'Debrief'),
          StringStruct('OriginalFilename', 'Debrief.exe'),
          StringStruct('ProductName', 'Debrief'),
          StringStruct('ProductVersion', '{VERSION}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # one-folder build: the DLLs and data sit next to the exe
    name="Debrief",
    icon=str(PKG / "desktop" / "debrief.ico"),
    version=str(VERSION_FILE),
    console=False,               # windowed: no black console box
    debug=False,
    strip=False,
    upx=False,                   # UPX-packed exes trip antivirus more often
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Debrief",
)
print(f"Debrief {VERSION}: spec loaded from {ROOT}")
