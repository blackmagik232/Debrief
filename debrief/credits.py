"""Credits (2.18): every third-party part Debrief uses or ships, with its author, licence and link.

One list for three places: CREDITS.md at the repository root, Help > Credits in the window (sent with /api/brand)
and THIRD_PARTY_LICENSES.txt, which holds the licence texts and notices the MIT, BSD, Apache-2.0, MPL-2.0, PSF
and OFL licences ask to be passed on. tests/test_218_credits.py checks the three agree and that the build
(packaging/debrief.spec) bundles both files.

    CREDITS        [{"name", "author", "licence", "link", "use", "kind"}]
                   kind: "bundled" (ships inside Debrief.exe), "voices" (made with it; ships as sound clips),
                   "ideas" (ideas only: no code, sounds or files), "build" (used to build Debrief; not shipped),
                   "optional" (used only when it is already on the PC; not in requirements.txt)
    COPYRIGHT      "© 2026 Black Magik Studios"
    as_list()      a copy of CREDITS, for the window

Pure data: no imports beyond brand, nothing read or written.
"""
from __future__ import annotations

from . import brand

COPYRIGHT = brand.COPYRIGHT

CREDITS = [
    # ---------------------------------------------------------------- shipped inside Debrief.exe
    {"name": "Python", "author": "Python Software Foundation", "licence": "PSF License 2.0",
     "link": "https://www.python.org", "use": "The language Debrief is written in; the installed app carries its "
     "own copy.", "kind": "bundled"},
    {"name": "requests", "author": "Kenneth Reitz", "licence": "Apache-2.0",
     "link": "https://github.com/psf/requests", "use": "Talks to Garage 61 and checks for updates.",
     "kind": "bundled"},
    {"name": "urllib3", "author": "Andrey Petrov and contributors", "licence": "MIT",
     "link": "https://github.com/urllib3/urllib3", "use": "Used by requests.", "kind": "bundled"},
    {"name": "certifi", "author": "Kenneth Reitz", "licence": "MPL-2.0",
     "link": "https://github.com/certifi/python-certifi", "use": "The trusted certificates requests checks "
     "websites with.", "kind": "bundled"},
    {"name": "charset-normalizer", "author": "Ahmed R. Tahri", "licence": "MIT",
     "link": "https://github.com/jawah/charset_normalizer", "use": "Used by requests.", "kind": "bundled"},
    {"name": "idna", "author": "Kim Davies and contributors", "licence": "BSD-3-Clause",
     "link": "https://github.com/kjd/idna", "use": "Used by requests.", "kind": "bundled"},
    {"name": "NumPy", "author": "NumPy Developers", "licence": "BSD-3-Clause (and the licences of the parts it "
     "bundles, such as OpenBLAS)", "link": "https://numpy.org", "use": "The number crunching behind the reviews, "
     "Practice, Setup Lab and the Coach.", "kind": "bundled"},
    {"name": "pypdf", "author": "Mathieu Fenniak and contributors", "licence": "BSD-3-Clause",
     "link": "https://github.com/py-pdf/pypdf", "use": "Reads iRacing's season schedule PDF.", "kind": "bundled"},
    {"name": "pyirsdk", "author": "Mihail Latyshov (kutu)", "licence": "MIT",
     "link": "https://github.com/kutu/pyirsdk", "use": "Reads iRacing's live data for the Coach, the background "
     "recorder and the replay scan. The recorder also follows its idea of re-reading the session text only when iRacing's "
     "update counter changes.", "kind": "bundled"},
    {"name": "PyYAML", "author": "Kirill Simonov and Ingy döt Net", "licence": "MIT",
     "link": "https://pyyaml.org", "use": "Reads the session details inside telemetry files.", "kind": "bundled"},
    {"name": "pywin32", "author": "Mark Hammond and contributors", "licence": "PSF-style (win32 parts: "
     "BSD-style)", "link": "https://github.com/mhammond/pywin32", "use": "Windows speech for the rare phrase a "
     "voice pack can't say, and the background recorder's icon near the clock.", "kind": "bundled"},
    {"name": "Vosk", "author": "Alpha Cephei Inc.", "licence": "Apache-2.0",
     "link": "https://github.com/alphacep/vosk-api", "use": "Offline speech recognition for Talk to your engineer: it hears "
     "your question on this PC and sends the sound nowhere.", "kind": "bundled"},
    {"name": "Vosk small English model", "author": "Alpha Cephei Inc.", "licence": "Apache-2.0",
     "link": "https://alphacephei.com/vosk/models", "use": "The speech model (vosk-model-small-en-us-0.15, about 40 MB) "
     "Talk to your engineer downloads once, the first time you turn it on.", "kind": "optional"},
    {"name": "sounddevice", "author": "Matthias Geier", "licence": "MIT",
     "link": "https://github.com/spatialaudio/python-sounddevice", "use": "Opens the microphone while you hold the talk "
     "button.", "kind": "bundled"},
    {"name": "PortAudio", "author": "Ross Bencina, Phil Burk and contributors", "licence": "MIT",
     "link": "https://www.portaudio.com", "use": "The audio library inside sounddevice's Windows package.",
     "kind": "bundled"},
    {"name": "IBM Plex Sans", "author": "IBM Corp.", "licence": "SIL Open Font License 1.1",
     "link": "https://github.com/IBM/plex", "use": "The window's text font.", "kind": "bundled"},
    {"name": "Barlow Condensed", "author": "The Barlow Project Authors (Jeremy Tribby)",
     "licence": "SIL Open Font License 1.1", "link": "https://github.com/jpt/barlow", "use": "Headings.",
     "kind": "bundled"},
    {"name": "JetBrains Mono", "author": "The JetBrains Mono Project Authors", "licence": "SIL Open Font License 1.1",
     "link": "https://github.com/JetBrains/JetBrainsMono", "use": "Numbers and labels.", "kind": "bundled"},
    {"name": "Fontsource", "author": "The Fontsource contributors", "licence": "MIT (the font files keep their "
     "own licence)", "link": "https://fontsource.org", "use": "Where the three font files were taken from.",
     "kind": "bundled"},
    {"name": "RFC 8032 (Ed25519) reference code", "author": "S. Josefsson and I. Liusvaara; IETF Trust",
     "licence": "Simplified BSD License", "link": "https://www.rfc-editor.org/rfc/rfc8032",
     "use": "Debrief's own Python port of it checks the signature on updates.", "kind": "bundled"},
    # ---------------------------------------------------------------- made with it, shipped as sound
    {"name": "Kokoro-82M", "author": "hexgrad", "licence": "Apache-2.0",
     "link": "https://huggingface.co/hexgrad/Kokoro-82M", "use": "The coach's voices (George: bm_george; "
     "Michael: am_michael; Fenrir: am_fenrir; Heart: af_heart) were generated with it once and ship as sound "
     "clips. Kokoro itself is not part of Debrief.", "kind": "voices"},
    # ---------------------------------------------------------------- ideas only
    {"name": "Crew Chief", "author": "Jim Britton and the Crew Chief contributors", "licence": "Not needed: ideas only, no code used",
     "link": "https://gitlab.com/mr_belowski/CrewChiefV4", "use": "Ideas for how a spoken coach should behave in "
     "the car. Ideas only: no code or sounds were taken from it. The 2.29 crew chief and the 2.30 Race Room swap heads-ups follow the same spirit.", "kind": "ideas"},
    # ---------------------------------------------------------------- used only if already installed
    {"name": "comtypes", "author": "Thomas Heller and the comtypes developers", "licence": "MIT",
     "link": "https://github.com/enthought/comtypes", "use": "A fallback for Windows speech, used only when it is "
     "already installed and pywin32 isn't.", "kind": "optional"},
    # ---------------------------------------------------------------- used to build Debrief, not shipped
    {"name": "PyInstaller", "author": "The PyInstaller team", "licence": "GPL-2.0 with the bootloader exception",
     "link": "https://pyinstaller.org", "use": "Turns Debrief into Debrief.exe. The exception means the app "
     "itself isn't bound by the GPL.", "kind": "build"},
    {"name": "Inno Setup", "author": "Jordan Russell and Martijn Laan", "licence": "Inno Setup License",
     "link": "https://jrsoftware.org/isinfo.php", "use": "Makes the installer.", "kind": "build"},
    {"name": "kokoro-onnx", "author": "thewh1teagle", "licence": "MIT",
     "link": "https://github.com/thewh1teagle/kokoro-onnx", "use": "Runs Kokoro-82M when the voice packs are "
     "made (tools/make_voicepack.py).", "kind": "build"},
]

KINDS = {"bundled": "Part of Debrief", "voices": "Coach voices", "ideas": "Ideas",
         "optional": "Used if already installed", "build": "Used to build Debrief (not shipped)"}

TRADEMARKS = ("iRacing is a trademark of iRacing.com Motorsport Simulations, LLC, and Garage 61 belongs to its owners. "
              "Debrief is not made by, endorsed by or affiliated with either.")


def as_list() -> list:
    return [dict(c) for c in CREDITS]
