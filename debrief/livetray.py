"""The recorder's tray icon (2.27 "Live data"): Debrief.exe --recorder runs the background recorder with no window and
this notification-area icon. Right-click: "Open Debrief", "Pause recording" / "Resume recording", "Quit recorder".

Windows only, with pywin32's win32gui (already a Windows dependency of the coach's voice; nothing new to install). Every
Windows import is inside run(), so Linux, the tests and CI can import this file. The menu itself (TrayModel) has no
Windows code at all and is tested directly; run() takes the three pywin32 modules as arguments so a test can hand it
stand-ins.
"""
from __future__ import annotations

from pathlib import Path

OPEN, PAUSE, QUIT = 1001, 1002, 1003
WM_TRAY = 0x0400 + 20          # WM_USER + 20
TIP_MAX = 120                  # a tray tooltip holds 127 characters


class TrayModel:
    """What the tray shows and does, with no Windows in it."""

    def __init__(self, recorder, open_window, quit_cb):
        self.recorder, self.open_window, self.quit_cb = recorder, open_window, quit_cb

    def items(self) -> list:
        paused = self.recorder.is_paused()
        return [(OPEN, "Open Debrief"), (PAUSE, "Resume recording" if paused else "Pause recording"), (QUIT, "Quit recorder")]

    def tooltip(self) -> str:
        st = self.recorder.status_doc()
        return ("Debrief: " + str(st.get("text") or "recorder"))[:TIP_MAX]

    def activate(self, item_id: int) -> None:
        if item_id == OPEN:
            self.open_window()
        elif item_id == PAUSE:
            self.recorder.resume() if self.recorder.is_paused() else self.recorder.pause()
        elif item_id == QUIT:
            self.quit_cb()


def icon_path() -> Path | None:
    try:
        from . import app
        for p in (app.RES / "debrief" / "desktop" / "debrief.ico", Path(__file__).resolve().parent / "desktop" / "debrief.ico"):
            if p.is_file():
                return p
    except Exception:  # noqa: BLE001
        pass
    return None


def run(recorder, open_window, quit_cb=None, gui=None, con=None, api=None) -> int:
    """Show the icon and run the Windows message loop until Quit. Returns 0. Raises ImportError without pywin32 (the caller
    then runs the recorder with no icon)."""
    if gui is None:
        import win32api as api  # noqa: F811
        import win32con as con  # noqa: F811
        import win32gui as gui  # noqa: F811
    state = {"hwnd": None, "icon": None}

    def finish():
        try:
            gui.Shell_NotifyIcon(con.NIM_DELETE, (state["hwnd"], 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            gui.PostQuitMessage(0)
        except Exception:  # noqa: BLE001
            pass
        if quit_cb:
            quit_cb()

    model = TrayModel(recorder, open_window, finish)

    def nid(flags):
        return (state["hwnd"], 0, flags, WM_TRAY, state["icon"], model.tooltip())

    def add_icon():
        try:
            gui.Shell_NotifyIcon(con.NIM_ADD, nid(con.NIF_ICON | con.NIF_MESSAGE | con.NIF_TIP))
        except Exception:  # noqa: BLE001
            pass

    def menu(hwnd):
        m = gui.CreatePopupMenu()
        for i, label in model.items():
            gui.AppendMenu(m, con.MF_STRING, i, label)
        pos = gui.GetCursorPos()
        gui.SetForegroundWindow(hwnd)
        gui.TrackPopupMenu(m, con.TPM_LEFTALIGN | con.TPM_RIGHTBUTTON, pos[0], pos[1], 0, hwnd, None)
        gui.PostMessage(hwnd, con.WM_NULL, 0, 0)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == con.WM_RBUTTONUP:
                menu(hwnd)
            elif lparam == con.WM_LBUTTONDBLCLK:
                model.activate(OPEN)
            return 0
        if msg == con.WM_COMMAND:
            model.activate(wparam & 0xFFFF)
            return 0
        if msg == con.WM_TIMER:
            try:
                gui.Shell_NotifyIcon(con.NIM_MODIFY, nid(con.NIF_TIP))
            except Exception:  # noqa: BLE001
                pass
            if not recorder._thread or not recorder._thread.is_alive():    # a quit request ended the recorder
                finish()
            return 0
        if msg == state.get("restart"):                                   # Explorer restarted: put the icon back
            add_icon()
            return 0
        if msg == con.WM_DESTROY:
            gui.PostQuitMessage(0)
            return 0
        return gui.DefWindowProc(hwnd, msg, wparam, lparam)

    hinst = api.GetModuleHandle(None)
    wc = gui.WNDCLASS()
    wc.hInstance, wc.lpszClassName, wc.lpfnWndProc = hinst, "DebriefRecorderTray", wndproc
    atom = gui.RegisterClass(wc)
    state["hwnd"] = gui.CreateWindow(atom, "Debrief Recorder", 0, 0, 0, 0, 0, 0, 0, hinst, None)
    try:
        state["restart"] = gui.RegisterWindowMessage("TaskbarCreated")
    except Exception:  # noqa: BLE001
        state["restart"] = None
    ico = icon_path()
    try:
        state["icon"] = gui.LoadImage(0, str(ico), con.IMAGE_ICON, 0, 0, con.LR_LOADFROMFILE | con.LR_DEFAULTSIZE) if ico else None
    except Exception:  # noqa: BLE001
        state["icon"] = None
    if not state["icon"]:
        state["icon"] = gui.LoadIcon(0, con.IDI_APPLICATION)
    add_icon()
    gui.SetTimer(state["hwnd"], 1, 5000, None)
    state["wndproc"] = wndproc
    state["model"] = model
    run.last = state                      # for the tests
    gui.PumpMessages()
    return 0
