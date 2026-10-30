For running from source only.

These scripts are for developers running Debrief from a git checkout. If you just want to use Debrief, download the installer from https://github.com/blackmagik232/debrief/releases instead.

- `Setup.bat` creates `.venv` in the folder above and installs the requirements.
- `Install shortcut.bat` puts a Debrief icon on the desktop (it runs `Debrief.vbs`).
- `Debrief.vbs` opens the Debrief window with no console.
- `Check connection.bat`, `Review last race.bat`, `Open library.bat` and `Edit settings.bat` run the command-line tools.

Each script switches to the folder above `dev` first, so `settings.toml` and `.venv` stay next to the code.
