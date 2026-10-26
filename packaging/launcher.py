"""Entry point for the installed Debrief app (PyInstaller builds Debrief.exe from this file).

Debrief.exe                 opens the Debrief window
Debrief.exe --selftest      checks the app loads, writes selftest.json to the data folder, exits 0/1
Debrief.exe --no-browser    starts without a window and prints the address (for testing)
"""
import sys

from debrief.desktop import main

if __name__ == "__main__":
    sys.exit(main())
