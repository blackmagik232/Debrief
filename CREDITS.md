# Credits

**Debrief · by Black Magik Studios.** © 2026 Black Magik Studios.

Every third-party part Debrief uses or ships, who made it, its licence and where to find it. The same list is in the app under **Help > Credits** (it comes from `debrief/credits.py`). The licence texts and notices these parts ask to be passed on are in [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt); the installed app carries both files.

## Part of Debrief

| Name | Author | Licence | Link | What for |
|---|---|---|---|---|
| Python | Python Software Foundation | PSF License 2.0 | <https://www.python.org> | The language Debrief is written in; the installed app carries its own copy. |
| requests | Kenneth Reitz | Apache-2.0 | <https://github.com/psf/requests> | Talks to Garage 61 and checks for updates. |
| urllib3 | Andrey Petrov and contributors | MIT | <https://github.com/urllib3/urllib3> | Used by requests. |
| certifi | Kenneth Reitz | MPL-2.0 | <https://github.com/certifi/python-certifi> | The trusted certificates requests checks websites with. |
| charset-normalizer | Ahmed R. Tahri | MIT | <https://github.com/jawah/charset_normalizer> | Used by requests. |
| idna | Kim Davies and contributors | BSD-3-Clause | <https://github.com/kjd/idna> | Used by requests. |
| NumPy | NumPy Developers | BSD-3-Clause (and the licences of the parts it bundles, such as OpenBLAS) | <https://numpy.org> | The number crunching behind the reviews, Practice, Setup Lab and the Coach. |
| pypdf | Mathieu Fenniak and contributors | BSD-3-Clause | <https://github.com/py-pdf/pypdf> | Reads iRacing's season schedule PDF. |
| pyirsdk | Mihail Latyshov (kutu) | MIT | <https://github.com/kutu/pyirsdk> | Reads iRacing's live data for the Coach, the background recorder and the replay scan. The recorder also follows its idea of re-reading the session text only when iRacing's update counter changes. |
| PyYAML | Kirill Simonov and Ingy döt Net | MIT | <https://pyyaml.org> | Reads the session details inside telemetry files. |
| pywin32 | Mark Hammond and contributors | PSF-style (win32 parts: BSD-style) | <https://github.com/mhammond/pywin32> | Windows speech for the rare phrase a voice pack can't say, and the background recorder's icon near the clock. |
| Vosk | Alpha Cephei Inc. | Apache-2.0 | <https://github.com/alphacep/vosk-api> | Offline speech recognition for Talk to your engineer: it hears your question on this PC and sends the sound nowhere. |
| sounddevice | Matthias Geier | MIT | <https://github.com/spatialaudio/python-sounddevice> | Opens the microphone while you hold the talk button. |
| PortAudio | Ross Bencina, Phil Burk and contributors | MIT | <https://www.portaudio.com> | The audio library inside sounddevice's Windows package. |
| IBM Plex Sans | IBM Corp. | SIL Open Font License 1.1 | <https://github.com/IBM/plex> | The window's text font. |
| Barlow Condensed | The Barlow Project Authors (Jeremy Tribby) | SIL Open Font License 1.1 | <https://github.com/jpt/barlow> | Headings. |
| JetBrains Mono | The JetBrains Mono Project Authors | SIL Open Font License 1.1 | <https://github.com/JetBrains/JetBrainsMono> | Numbers and labels. |
| Fontsource | The Fontsource contributors | MIT (the font files keep their own licence) | <https://fontsource.org> | Where the three font files were taken from. |
| RFC 8032 (Ed25519) reference code | S. Josefsson and I. Liusvaara; IETF Trust | Simplified BSD License | <https://www.rfc-editor.org/rfc/rfc8032> | Debrief's own Python port of it checks the signature on updates. |

## Coach voices

| Name | Author | Licence | Link | What for |
|---|---|---|---|---|
| Kokoro-82M | hexgrad | Apache-2.0 | <https://huggingface.co/hexgrad/Kokoro-82M> | The coach's voices (George: bm_george; Michael: am_michael; Fenrir: am_fenrir; Heart: af_heart) were generated with it once and ship as sound clips. Kokoro itself is not part of Debrief. |

## Ideas

| Name | Author | Licence | Link | What for |
|---|---|---|---|---|
| Crew Chief | Jim Britton and the Crew Chief contributors | Not needed: ideas only, no code used | <https://gitlab.com/mr_belowski/CrewChiefV4> | Ideas for how a spoken coach should behave in the car. Ideas only: no code or sounds were taken from it. In 2.29 the crew chief's habits (short calls, nothing said twice in a row, quiet moments, a caution is not a pit stop) follow the same spirit. In 2.30 the Race Room's driver-swap heads-ups ("your stint starts in about five minutes") and its handover card follow the same spirit of short, useful calls. |
| Debrief's own Race Room and stint plan | Black Magik Studios | Part of Debrief | (this repository) | The pit board layout, the rival gap/closing-rate idea and the pit-window rule (one lap of reserve, as in strategy.py) were reused from Debrief's own earlier work. The 2.30 Race Room's stint timer, plan delta, live re-plan and Endurance review are Debrief's own, built on the 2.26 stint planner (endurance.py); the idea of reading every car's pit stops from the pit-road flag and of merging teammates' logs is Debrief's own. |

## Used if already installed

| Name | Author | Licence | Link | What for |
|---|---|---|---|---|
| comtypes | Thomas Heller and the comtypes developers | MIT | <https://github.com/enthought/comtypes> | A fallback for Windows speech, used only when it is already installed and pywin32 isn't. |
| Vosk small English model | Alpha Cephei Inc. | Apache-2.0 | <https://alphacephei.com/vosk/models> | The speech model (vosk-model-small-en-us-0.15, about 40 MB) Talk to your engineer downloads once, the first time you turn it on. |

## Used to build Debrief (not shipped)

| Name | Author | Licence | Link | What for |
|---|---|---|---|---|
| PyInstaller | The PyInstaller team | GPL-2.0 with the bootloader exception | <https://pyinstaller.org> | Turns Debrief into Debrief.exe. The exception means the app itself isn't bound by the GPL. |
| Inno Setup | Jordan Russell and Martijn Laan | Inno Setup License | <https://jrsoftware.org/isinfo.php> | Makes the installer. |
| kokoro-onnx | thewh1teagle | MIT | <https://github.com/thewh1teagle/kokoro-onnx> | Runs Kokoro-82M when the voice packs are made (tools/make_voicepack.py). |

## Trademarks

iRacing is a trademark of iRacing.com Motorsport Simulations, LLC, and Garage 61 belongs to its owners. Debrief is not made by, endorsed by or affiliated with either.
