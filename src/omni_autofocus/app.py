"""Omni Autofocus app: a small always-on-top window with an Autofocus button and a global hotkey,
meant to sit next to LightBurn. Windows only; uses only the standard library (tkinter, ctypes).

All laser work runs on a worker thread through the same code as the command line
(:mod:`omni_autofocus.session` and :func:`omni_autofocus.autofocus.run`); the window only shows
progress and asks questions.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes as wt
from pathlib import Path

from . import __version__, autofocus, config, session
from .controller import ControllerBusyError, ControllerError, SensorNoTargetError

log = logging.getLogger(__name__)

APP_NAME = "Omni Autofocus"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "OmniAutofocus"
MUTEX_NAME = "Local\\OmniAutofocusApp"
LENS_CHOICES = {"Auto": None, "Lens A": "a", "Lens B": "b"}

OK, WARN, ERR, INFO = "#1a7f37", "#9a6700", "#cf222e", ""


# --- pure helpers (tested without a window) -----------------------------------------------------------


def describe_error(e: BaseException) -> str:
    """A short, actionable sentence for anything an operation can raise."""
    if isinstance(e, ControllerBusyError):
        return "The laser is busy. Stop the LightBurn job or close the framing preview, then try again."
    if isinstance(e, SensorNoTargetError):
        return (
            "The sensor sees no surface in range. Move the head to roughly working height with the "
            "machine's Z buttons, then try again."
        )
    if isinstance(e, session.ConflictError):
        return str(e)
    if isinstance(e, autofocus.FocusError):
        msg = str(e)
        return msg[0].upper() + msg[1:] + "."
    return str(e) or type(e).__name__


def needs_confirmation(p: autofocus.FocusPlan, app: config.AppSettings) -> bool:
    """One-click focus asks only before a downward move larger than the configured threshold."""
    return p.move_mm < -abs(app.confirm_down_above_mm)


def describe_result(r: autofocus.FocusResult) -> tuple[str, str]:
    """(status line, colour) for a finished autofocus."""
    if r.outcome is autofocus.Outcome.CANCELLED:
        return "Cancelled. Z did not move.", WARN
    if r.outcome is autofocus.Outcome.NOT_CONVERGED:
        return f"Still {r.error_mm:+.2f} mm from focus. Check the Z axis and try again.", ERR
    if r.moved:
        return f"In focus (moved {sum(p.move_mm for p in r.plans):+.1f} mm).", OK
    return "Already in focus.", OK


def run_command(frozen: bool, executable: str) -> str:
    """The command line that starts the app (for the Windows Run key)."""
    if frozen:
        return f'"{executable}"'
    pythonw = Path(executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else executable}" -m omni_autofocus.app'


def cli_command(frozen: bool, executable: str) -> list[str] | None:
    """How to start the command-line tool (installed next to the app's .exe)."""
    if not frozen:
        return [executable.replace("pythonw.exe", "python.exe"), "-m", "omni_autofocus"]
    cli = Path(executable).with_name("omni-autofocus.exe")
    return [str(cli)] if cli.exists() else None


# --- Windows integration --------------------------------------------------------------------------------


class Hotkey(threading.Thread):
    """A system-wide hotkey (``RegisterHotKey``) with its own message loop."""

    WM_HOTKEY, WM_QUIT, MOD_NOREPEAT = 0x0312, 0x0012, 0x4000

    def __init__(self, mods: int, vk: int, callback):
        super().__init__(daemon=True, name="hotkey")
        self.mods, self.vk, self.callback = mods, vk, callback
        self.ready = threading.Event()
        self.ok = False
        self._thread_id = 0

    def run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.ok = bool(user32.RegisterHotKey(None, 1, self.mods | self.MOD_NOREPEAT, self.vk))
        self.ready.set()
        if not self.ok:
            return
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)


def start_with_windows_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except OSError:
        return False


def set_start_with_windows(enabled: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enabled:
            cmd = run_command(getattr(sys, "frozen", False), sys.executable)
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def _already_running() -> bool:
    """Hold a named mutex for the app's lifetime; True if another instance holds it."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    global _MUTEX
    _MUTEX = k32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def _show_existing_window() -> None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wt.HWND
    hwnd = user32.FindWindowW(None, APP_NAME)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _virtual_screen() -> tuple[int, int, int, int]:
    m = ctypes.windll.user32.GetSystemMetrics
    return m(76), m(77), m(78), m(79)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CX..., SM_CY...


def _beep(ok: bool) -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except (ImportError, RuntimeError):
        pass


def _state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "omni-autofocus" / "app-state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


# --- the window --------------------------------------------------------------------------------------


class App:
    def __init__(self, *, config_path: Path | None, simulate: bool):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.config_path, self.simulate = config_path, simulate
        self._session = session.Session(config_path=config_path, simulate=simulate)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.hotkey: Hotkey | None = None
        self.app_settings = config.AppSettings()
        self.state = _load_state()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        icon = Path(__file__).with_name("assets") / "icon.ico"
        if icon.exists():
            self.root.iconbitmap(default=str(icon))
        self.root.resizable(False, False)
        style = ttk.Style(self.root)
        style.configure("Big.TButton", font=("Segoe UI", 14, "bold"), padding=(12, 10))

        frame = ttk.Frame(self.root, padding=10)
        frame.grid(sticky="nsew")
        self.button = ttk.Button(frame, text="Autofocus", style="Big.TButton", command=self.autofocus)
        self.button.grid(row=0, column=0, columnspan=3, sticky="ew")

        ttk.Label(frame, text="Lens").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.lens_var = tk.StringVar(value="Auto")
        lens_menu = ttk.OptionMenu(frame, self.lens_var, "Auto", *LENS_CHOICES, command=self._lens_changed)
        lens_menu.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.more = ttk.Menubutton(frame, text="⋯", width=3)
        self.more.grid(row=1, column=2, sticky="e", pady=(8, 0))

        self.lens_info = ttk.Label(frame, text="", foreground="gray", wraplength="200p")
        self.lens_info.grid(row=2, column=0, columnspan=3, sticky="w")
        self.status = ttk.Label(frame, text="Starting…", wraplength="200p")
        self.status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.detail = ttk.Label(frame, text="", foreground="gray", wraplength="200p")
        self.detail.grid(row=4, column=0, columnspan=3, sticky="w")
        frame.columnconfigure(1, weight=1)

        self.on_top = tk.BooleanVar(value=self.state.get("on_top", True) is not False)
        self.autostart = tk.BooleanVar(value=False)
        menu = tk.Menu(self.more, tearoff=False)
        menu.add_command(label="Check height", command=self.check_height)
        menu.add_command(label="Fine-tune focus (test burns)…", command=self.open_ladder)
        menu.add_separator()
        menu.add_checkbutton(label="Always on top", variable=self.on_top, command=self._toggle_on_top)
        menu.add_checkbutton(
            label="Start with Windows", variable=self.autostart, command=self._toggle_autostart
        )
        menu.add_separator()
        menu.add_command(label="Check USB driver…", command=self.check_driver)
        menu.add_command(label="Open settings file", command=self.open_settings)
        menu.add_command(label="About", command=self.about)
        self.more["menu"] = menu

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Return>", lambda e: self.autofocus())
        self._restore_position()
        self.root.attributes("-topmost", self.on_top.get())
        if sys.platform == "win32":
            self.autostart.set(start_with_windows_enabled())
        self.root.after(50, self._pump)
        self._start(self._prepare)

    # -- lifecycle ------------------------------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        if self.busy and not self._ask_yes_no("Z may still be moving. Quit anyway?"):
            return
        if self.hotkey:
            self.hotkey.stop()
        _save_state({"x": self.root.winfo_x(), "y": self.root.winfo_y(), "on_top": self.on_top.get()})
        self.root.destroy()

    def _restore_position(self) -> None:
        x, y = self.state.get("x"), self.state.get("y")
        if isinstance(x, int) and isinstance(y, int) and sys.platform == "win32":
            vx, vy, vw, vh = _virtual_screen()
            if vx <= x <= vx + vw - 100 and vy <= y <= vy + vh - 60:
                self.root.geometry(f"+{x}+{y}")

    def _apply_app_settings(self, app: config.AppSettings) -> None:
        self.app_settings = app
        if self.hotkey is None and app.hotkey and sys.platform == "win32":
            try:
                mods, vk = config.parse_hotkey(app.hotkey)
            except ValueError as e:
                self._set_detail(f"Hotkey off: {e}")
                return
            self.hotkey = Hotkey(mods, vk, lambda: self.events.put(("hotkey",)))
            self.hotkey.start()
            self.hotkey.ready.wait(1.0)
            if self.hotkey.ok:
                self.button.configure(text=f"Autofocus   ({app.hotkey.title()})")
            else:
                self.hotkey = None
                self._set_detail(f"Hotkey {app.hotkey} is used by another program (see app.hotkey).")

    # -- worker plumbing ------------------------------------------------------------------------------

    def _start(self, job, *args) -> None:
        if self.busy:
            self._set_status("Still working…", WARN)
            return
        self.busy = True
        self.button.state(["disabled"])
        threading.Thread(target=self._work, args=(job, *args), daemon=True).start()

    def _work(self, job, *args) -> None:
        try:
            job(*args)
        except Exception as e:  # noqa: BLE001 - every failure becomes a status line
            log.debug("operation failed", exc_info=True)
            self.events.put(("status", describe_error(e), ERR))
            self.events.put(("beep", False))
        finally:
            self.events.put(("idle",))

    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self._set_status(event[1], event[2])
                elif kind == "detail":
                    self._set_detail(event[1])
                elif kind == "lens":
                    self.lens_info.configure(text=event[1])
                elif kind == "app":
                    self._apply_app_settings(event[1])
                elif kind == "ask":
                    _, question, answer, done = event
                    answer.append(self._ask_yes_no(question))
                    done.set()
                elif kind == "beep":
                    if self.app_settings.sounds:
                        _beep(event[1])
                elif kind == "hotkey":
                    self.autofocus()
                elif kind == "idle":
                    self.busy = False
                    self.button.state(["!disabled"])
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _ask(self, question: str) -> bool:
        """Ask from the worker thread; blocks until the user answers in the window."""
        answer: list[bool] = []
        done = threading.Event()
        self.events.put(("ask", question, answer, done))
        done.wait()
        return answer[0]

    def _ask_yes_no(self, question: str) -> bool:
        from tkinter import messagebox

        self.root.deiconify()
        self.root.lift()
        return messagebox.askyesno(APP_NAME, question, parent=self.root)

    def _set_status(self, text: str, colour: str = INFO) -> None:
        self.status.configure(text=text, foreground=colour)

    def _set_detail(self, text: str) -> None:
        self.detail.configure(text=text)

    def _post(self, *event) -> None:
        self.events.put(event)

    # -- operations (worker thread) ---------------------------------------------------------------------

    def _settings(self, *, calibrate: bool = True) -> config.Settings:
        calibrated = calibrate or self._session.path.exists()
        s, why, note = self._session.settings_with_lens(calibrate=calibrate)
        self._post("app", s.app)
        focus = (
            f"Focus at sensor reading {s.focus.target_mm:.1f} mm."
            if calibrated
            else "Focus heights are read from the laser on first use."
        )
        self._post("lens", f"{why[0].upper()}{why[1:]}. {focus}")
        if note:
            self._post("detail", note)
        return s

    def _prepare(self) -> None:
        self._post("status", "Ready. Place the work piece, then press Autofocus.", INFO)
        try:
            self._settings(calibrate=False)  # never touch the laser just because the app started
        except (ControllerError, OSError, ValueError) as e:
            self._post("status", "Ready.", INFO)
            self._post("detail", describe_error(e))
            try:
                s, _ = self._session.settings(calibrate=False)
                self._post("app", s.app)
            except (OSError, ValueError):
                self._post("app", config.AppSettings())

    def autofocus(self) -> None:
        self._start(self._autofocus)

    def _autofocus(self) -> None:
        note = self._session.check_other_software()
        self._post("status", "Measuring…", INFO)
        self._post("detail", note or "")
        s = self._settings()

        def on_plan(i: int, p: autofocus.FocusPlan) -> None:
            if p.needed:
                self._post("status", f"Moving Z {p.move_mm:+.1f} mm…", INFO)
            self._post("detail", f"Sensor {p.height_mm:.1f} mm, target {p.target_mm:.1f} mm")

        def confirm(p: autofocus.FocusPlan) -> bool:
            if not needs_confirmation(p, s.app):
                return True
            return self._ask(
                f"Move the head DOWN {-p.move_mm:.1f} mm, towards the work?\n\n"
                f"Sensor reads {p.height_mm:.1f} mm, focus is at {p.target_mm:.1f} mm."
            )

        dev, ctl = self._session.open()
        with dev:
            result = autofocus.run(ctl, s, passes=2, confirm=confirm, on_plan=on_plan)
        text, colour = describe_result(result)
        self._post("status", text, colour)
        self._post("detail", f"Sensor {result.height_mm:.1f} mm, error {result.error_mm:+.2f} mm")
        self._post("beep", colour == OK)

    def check_height(self) -> None:
        self._start(self._check_height)

    def _check_height(self) -> None:
        self._session.check_other_software()
        s = self._settings()
        dev, ctl = self._session.open()
        with dev:
            h = ctl.read_height_median(s.focus.samples)
        move = s.focus.target_mm - h
        self._post("status", f"Height {h:.1f} mm.", INFO)
        self._post("detail", f"Focus is at {s.focus.target_mm:.1f} mm; Autofocus would move {move:+.1f} mm.")

    # -- menu actions (UI thread) -----------------------------------------------------------------------

    def _lens_changed(self, _value=None) -> None:
        # Set here, on the UI thread; the worker only reads the session.
        self._session.lens_override = LENS_CHOICES.get(self.lens_var.get())
        if not self.busy:
            self._start(self._prepare)

    def _toggle_on_top(self) -> None:
        self.root.attributes("-topmost", self.on_top.get())

    def _toggle_autostart(self) -> None:
        try:
            set_start_with_windows(self.autostart.get())
        except OSError as e:
            self.autostart.set(not self.autostart.get())
            self._set_status(f"Could not change the startup setting: {e}", ERR)

    def open_ladder(self) -> None:
        cmd = cli_command(getattr(sys, "frozen", False), sys.executable)
        if cmd is None:
            self._set_status("omni-autofocus.exe was not found next to the app.", ERR)
            return
        args = ["--simulate"] if self.simulate else []
        if self.config_path:
            args += ["--config", str(self.config_path)]
        lens = LENS_CHOICES.get(self.lens_var.get())
        if lens:
            args += ["--lens", lens]
        subprocess.Popen(
            [*cmd, *args, "focus-ladder"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        )
        self._set_status("Fine-tuning runs in its own window.", INFO)

    def check_driver(self) -> None:
        from . import driver

        try:
            diag = driver.diagnose(driver.usb_devices())
        except OSError as e:
            self._set_status(str(e), ERR)
            return
        if diag.state in (driver.State.OK, driver.State.NO_LASER):
            self._info(diag.message)
            return
        installers = driver.find_installers()
        installer = installers[0] if installers else None
        lines = driver.guidance(diag, staged=driver.driver_staged(), installer=installer)
        text = diag.message + "\n\n" + "\n".join(lines)
        if installer and self._ask_yes_no(text + f"\n\nRun {installer.name} now?"):
            self._start(self._run_driver_installer, installer)
        elif not installer:
            self._info(text)

    def _run_driver_installer(self, installer: Path) -> None:
        from . import driver

        self._post("status", "Waiting for the driver installer…", INFO)
        driver.run_installer(installer)
        self._post("status", "Installer finished. Unplug and replug the laser's USB cable.", WARN)

    def open_settings(self) -> None:
        path = self._session.path
        if not path.exists():
            self._info("There is no settings file yet. It is created the first time the laser is used.")
            return
        os.startfile(path)  # noqa: S606 - opens the user's own settings file in their editor

    def about(self) -> None:
        self._info(
            f"{APP_NAME} {__version__}\n\n"
            "Autofocus for the ComMarker Omni X / Xe next to LightBurn.\n"
            "By Sean (github.com/srm985). Free to use with credit (MIT licence).\n\n"
            "Not affiliated with ComMarker, BSL or LightBurn Software."
        )

    def _info(self, text: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo(APP_NAME, text, parent=self.root)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="omni-autofocus-app", description=APP_NAME)
    p.add_argument("--config", help="settings file (default: %%APPDATA%%\\omni-autofocus\\config.toml)")
    p.add_argument("--simulate", action="store_true", help="use a simulated laser")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if args.simulate and not args.config:
        args.config = str(Path(tempfile.gettempdir()) / "omni-autofocus-simulate.toml")
    if sys.platform == "win32":
        if _already_running():
            _show_existing_window()
            return 0
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        except (AttributeError, OSError):
            pass
    App(config_path=Path(args.config) if args.config else None, simulate=args.simulate).run()
    return 0


_MUTEX = None

if __name__ == "__main__":
    sys.exit(main())
