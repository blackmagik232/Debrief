"""Entry point for the installed Debrief app (PyInstaller builds Debrief.exe from this file).

Debrief.exe                 opens the Debrief window
Debrief.exe --selftest      checks the app loads, writes selftest.json to the data folder, exits 0/1
Debrief.exe --no-browser    starts without a window and prints the address (for testing)
Debrief.exe --recorder      no window: records iRacing sessions in the background, with a tray icon (2.27). The
                            "Start with Windows" setting runs this at sign-in (Run key "Debrief Recorder").
Debrief.exe --stop-recorder asks a running recorder to quit (the installer calls it before replacing files)

All of them go through debrief.desktop.main(), which reads the flags.
"""
import os
import sys

if "--selftest" in sys.argv:
    # a selftest must never hang a build: from the very first import, dump every thread's stack to
    # selftest-hang.txt in the data folder and exit if it takes longer than 120 s
    import faulthandler
    try:
        _d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Debrief")
        os.makedirs(_d, exist_ok=True)
        _hang = open(os.path.join(_d, "selftest-hang.txt"), "w", encoding="utf-8")
        _hang.write("started\n")
        _hang.flush()
        faulthandler.dump_traceback_later(120, exit=True, file=_hang)
    except Exception:  # noqa: BLE001
        pass

from debrief.desktop import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
