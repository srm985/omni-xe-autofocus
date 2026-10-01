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
import sys
import tempfile
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

from . import __version__, autofocus, config, ladder, lens, session
from .controller import ControllerBusyError, ControllerError, SensorNoTargetError

log = logging.getLogger(__name__)

APP_NAME = "Omni Autofocus"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "OmniAutofocus"
MUTEX_NAME = "Local\\OmniAutofocusApp"
LENS_CHOICES = {"Auto": None, "A": "a", "B": "b"}

# Status tones; ui.Palette has a colour for each.
OK, WARN, ERR, INFO, BUSY = "ok", "warn", "err", "info", "busy"


# --- pure helpers (tested without a window) -----------------------------------------------------------


class Cancelled(Exception):
    """The user said no to a question; nothing moved."""


def describe_error(e: BaseException) -> str:
    """A short, actionable sentence for anything an operation can raise."""
    if isinstance(e, lens.LensError):
        return "Cannot tell which lens is fitted. Choose Lens A or Lens B in this window."
    if isinstance(e, ControllerBusyError):
        return "The laser is busy. Stop the LightBurn job or close the framing preview, then try again."
    if isinstance(e, SensorNoTargetError):
        return (
            "The sensor sees no surface in range. Move the head to roughly working height with the "
            "machine's Z buttons, then try again."
        )
    if isinstance(e, session.ConflictError):
        msg = str(e)
        return msg[0].upper() + msg[1:]
    if isinstance(e, autofocus.FocusError):
        msg = str(e)
        return msg[0].upper() + msg[1:] + "."
    return str(e) or type(e).__name__


def needs_confirmation(
    p: autofocus.FocusPlan, app: config.AppSettings, *, motion_fault: bool = False
) -> bool:
    """One-click focus asks only before a downward move larger than the configured threshold, or
    before every move after Z last failed to follow a move."""
    return motion_fault or autofocus.is_large_downward(p, app.confirm_down_above_mm)


def updown(mm: float) -> str:
    """'up 17.0 mm' / 'down 3.0 mm': plain words instead of signs."""
    return f"{'up' if mm >= 0 else 'down'} {abs(mm):.1f} mm"


def describe_result(r: autofocus.FocusResult) -> tuple[str, str, str]:
    """(headline, detail, tone) for a finished autofocus."""
    if r.outcome is autofocus.Outcome.CANCELLED:
        return "Cancelled", ("Stopped after the first correction." if r.moved else "Z did not move."), INFO
    if r.outcome is autofocus.Outcome.NOT_CONVERGED:
        off = f"Still {abs(r.error_mm):.1f} mm off · height {r.height_mm:.1f} mm."
        return "Not in focus", off + " Check the Z axis.", ERR
    if r.moved:
        return (
            "In focus",
            f"Moved {updown(r.height_mm - r.plans[0].height_mm)} · height {r.height_mm:.1f} mm",  # measured
            OK,
        )
    return "Already in focus", f"Height {r.height_mm:.1f} mm · focus {r.target_mm:.1f} mm", OK


def lens_summary(choice: str, why: str, target_mm: float | None) -> str:
    """One quiet line, e.g. "Auto → B · LightBurn 'BSLFiber' · 222.0 mm"."""
    letter = why.split(" (")[0].split()[-1].upper()
    head = f"Auto → {letter}" if choice == "Auto" else f"Lens {letter}"
    if "LightBurn" in why:
        name = why.split("'")[1] if why.count("'") >= 2 else ""
        name = name if len(name) <= 24 else name[:23] + "…"
        source = f"LightBurn '{name}'" if name else "LightBurn"
    elif "ComMarker" in why:
        source = "ComMarker Studio"
    elif "simulation" in why:
        source = "simulation"
    else:
        source = ""
    focus = f"{target_mm:.1f} mm" if target_mm is not None else "focus read on first use"
    return " · ".join(part for part in (head, source, focus) if part)


TUNE_OFFSETS = tuple(ladder.default_offsets())  # -4 .. +4 mm in 1 mm steps


def fmt_offset(mm: float) -> str:
    """'+2', '0', '−3' (a real minus sign)."""
    return "0" if mm == 0 else f"{mm:+g}".replace("-", "−")


def parse_offset_label(label: str) -> float:
    return float(label.replace("−", "-"))


def window_title(simulate: bool) -> str:
    return f"{APP_NAME} (simulation)" if simulate else APP_NAME


def run_command(frozen: bool, executable: str) -> str:
    """The command line that starts the app (for the Windows Run key)."""
    if frozen:
        return f'"{executable}"'
    pythonw = Path(executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else executable}" -m omni_autofocus.app'


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


def _already_running(name: str) -> bool:
    """Hold a named mutex for the app's lifetime; True if another instance holds it."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    global _MUTEX
    _MUTEX = k32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def _show_existing_window(title: str) -> None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    hwnd = user32.FindWindowW("TkTopLevel", title)  # Tk's top-level window class
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _virtual_screen() -> tuple[int, int, int, int]:
    m = ctypes.windll.user32.GetSystemMetrics
    return m(76), m(77), m(78), m(79)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CX..., SM_CY...


def _wait_for_keys_released(timeout_s: float = 2.0) -> None:
    """Return once Enter and Space are up (or after ``timeout_s``)."""
    if sys.platform != "win32":
        return
    state = ctypes.windll.user32.GetAsyncKeyState
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and any(state(vk) & 0x8000 for vk in (0x0D, 0x20)):
        time.sleep(0.02)


def _beep(ok: bool) -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except (ImportError, RuntimeError):
        pass


def _state_path(simulate: bool = False) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    name = (
        "app-state-simulation.json" if simulate else "app-state.json"
    )  # simulation never touches the real one
    return Path(base) / "omni-autofocus" / name


def _load_state(simulate: bool = False) -> dict:
    try:
        return json.loads(_state_path(simulate).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, simulate: bool = False) -> None:
    try:
        path = _state_path(simulate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


# --- the window --------------------------------------------------------------------------------------


class App:
    WIDTH = 300  # content width in design pixels (scaled for the screen)
    QUIET_AFTER_DIALOG_S = 0.6  # ignore the Autofocus key briefly after a dialog (held/repeated Enter)

    def __init__(self, *, config_path: Path | None, simulate: bool):
        import tkinter as tk

        from . import ui

        self.tk = tk
        self.config_path, self.simulate = config_path, simulate
        self._session = session.Session(config_path=config_path, simulate=simulate)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.moving = False  # an operation that can move Z is running
        self.hotkey: Hotkey | None = None
        self._hotkey_tried = False
        self.hotkey_text = ""  # e.g. "Ctrl + Alt + F" once registered
        self.app_settings = config.AppSettings()
        self.state = _load_state(simulate)
        saved_lens = self.state.get("lens")
        self.lens_choice = saved_lens if saved_lens in LENS_CHOICES else "Auto"
        self._session.lens_override = LENS_CHOICES[self.lens_choice]
        self._lens_dirty = False
        self._tone = INFO
        self.motion_fault = bool(self.state.get("motion_fault"))
        self._verified_axis = None  # z_axis settings Z was last seen to follow
        self._quiet_until = 0.0
        self.tune: dict | None = None  # fine-tuning state while its view is open

        self.root = tk.Tk()
        self.title = window_title(simulate)
        self.root.title(self.title)
        icon = Path(__file__).with_name("assets") / "icon.ico"
        if icon.exists():
            self.root.iconbitmap(default=str(icon))
        self.root.resizable(False, False)
        dark = ui.windows_prefers_dark()
        p = self.palette = ui.DARK if dark else ui.LIGHT
        sc = ui.scale_of(self.root)
        wrap = int(self.WIDTH * sc)
        font = ui.FONT
        self.root.configure(bg=p.bg)

        body = tk.Frame(self.root, bg=p.bg, padx=int(20 * sc), pady=int(16 * sc))
        body.pack(fill="both", expand=True)

        # Status: a coloured dot, a one-line headline, two quiet lines of detail (fixed heights, so
        # the button never moves under the pointer).
        head = tk.Frame(body, bg=p.bg)
        head.pack(fill="x")
        d = int(10 * sc)
        self.dot = tk.Canvas(head, width=d, height=d, bg=p.bg, highlightthickness=0, bd=0)
        self._dot = self.dot.create_oval(1, 1, d - 1, d - 1, fill=p.info, outline="")
        self.dot.pack(side="left", padx=(0, int(8 * sc)))
        self.status = tk.Label(
            head, text="Starting…", bg=p.bg, fg=p.text, font=(font, 12, "bold"), anchor="w"
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.more = tk.Label(head, text="⋯", bg=p.bg, fg=p.muted, font=(font, 14), cursor="hand2")
        self.more.configure(padx=int(8 * sc), pady=int(2 * sc), takefocus=1, highlightthickness=2)
        self.more.configure(highlightcolor=p.text, highlightbackground=p.bg)
        ui.focus_ring(self.more, p.text, lambda: p.bg)
        self.more.pack(side="right")
        self.detail = tk.Label(
            body, text=" ", bg=p.bg, fg=p.muted, font=(font, 9), anchor="nw", justify="left",
            wraplength=wrap, height=2,
        )  # fmt: skip
        self.detail.pack(fill="x", pady=(int(4 * sc), 0))

        self.main_part = main = tk.Frame(body, bg=p.bg)  # Autofocus view
        main.pack(fill="x")
        self.tune_part = tk.Frame(body, bg=p.bg)  # fine-tuning view, swapped in by open_fine_tune
        self.button = ui.RoundButton(
            main, text="Autofocus", command=self.autofocus, palette=p, scale=sc, width=self.WIDTH, height=46,
            guard=self._key_allowed,
        )  # fmt: skip
        self.button.pack(pady=(int(12 * sc), int(6 * sc)))
        self.hint = tk.Label(main, text=" ", bg=p.bg, fg=p.muted, font=(font, 9))
        self.hint.pack()

        tk.Frame(main, bg=p.border, height=1).pack(fill="x", pady=(int(14 * sc), int(12 * sc)))
        row = tk.Frame(main, bg=p.bg)
        row.pack(fill="x")
        tk.Label(row, text="Lens", bg=p.bg, fg=p.text, font=(font, 9)).pack(side="left")
        self.lens_picker = ui.Segmented(
            row,
            options=list(LENS_CHOICES),
            value=self.lens_choice,
            command=self._lens_changed,
            palette=p,
            scale=sc,
        )
        self.lens_picker.pack(side="right")
        self.lens_info = tk.Label(main, text=" ", bg=p.bg, fg=p.muted, font=(font, 8), anchor="w")
        self.lens_info.pack(fill="x", pady=(int(8 * sc), 0))
        self._build_tune(self.tune_part, p, sc, wrap, font)
        # Same width in both views, so the window does not jump when fine-tuning opens or closes.
        self.root.update_idletasks()
        inner = max(self.main_part.winfo_reqwidth(), self.tune_part.winfo_reqwidth())
        self.root.minsize(inner + 2 * int(20 * sc), 0)

        self.on_top = tk.BooleanVar(value=self.state.get("on_top", True) is not False)
        self.autostart = tk.BooleanVar(value=False)
        menu = tk.Menu(self.root, tearoff=False, bg=p.bg, fg=p.text, bd=0, font=(font, 9))
        menu.configure(activebackground=p.accent, activeforeground=p.on_accent, selectcolor=p.text)
        menu.configure(disabledforeground=p.muted)
        menu.add_command(label="Check height", command=self.check_height)
        menu.add_command(label="Fine-tune focus (test burns)…", command=self.open_fine_tune)
        menu.add_separator()
        menu.add_checkbutton(label="Always on top", variable=self.on_top, command=self._toggle_on_top)
        menu.add_checkbutton(
            label="Start with Windows", variable=self.autostart, command=self._toggle_autostart
        )
        menu.add_separator()
        menu.add_command(label="Check USB driver…", command=self.check_driver)
        menu.add_command(label="Open settings file", command=self.open_settings)
        menu.add_command(label="About", command=self.about)
        self.menu = menu
        for seq in ("<Button-1>", "<space>", "<Return>"):
            self.more.bind(seq, lambda e: self._show_menu())

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._restore_position()
        self.root.attributes("-topmost", self.on_top.get())
        ui.dark_title_bar(self.root, dark)
        if simulate:  # never let a simulation register the real app's hotkey or autostart
            menu.entryconfigure("Start with Windows", state="disabled")
            self.hint.configure(text="Simulation · no laser is used, hotkey off")
        elif sys.platform == "win32":
            self.autostart.set(start_with_windows_enabled())
        self.button.focus_set()
        self.root.after(50, self._pump)
        self._start(self._prepare)

    # -- lifecycle ------------------------------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        if self.tune is not None and self.tune["stage"] == "burn":
            self._set_detail("Stop fine-tuning first, so Z can return to the focus height.")
            return
        if self.moving:  # quitting would abandon a move half-way; the installer's close request waits
            self._set_status("Still moving", WARN)
            self._set_detail("Wait until Z has stopped, then close the window.")
            return
        if self.hotkey:
            self.hotkey.stop()
        if self.tune is not None:
            self._close_tune()
        _save_state(
            {
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
                "on_top": self.on_top.get(),
                "motion_fault": self.motion_fault,
                "lens": self.lens_choice,
            },
            self.simulate,
        )
        self.root.destroy()

    def _restore_position(self) -> None:
        x, y = self.state.get("x"), self.state.get("y")
        if isinstance(x, int) and isinstance(y, int) and sys.platform == "win32":
            vx, vy, vw, vh = _virtual_screen()
            if vx <= x <= vx + vw - 100 and vy <= y <= vy + vh - 60:
                self.root.geometry(f"+{x}+{y}")

    def _show_menu(self) -> None:
        m = self.more
        self.menu.update_idletasks()
        x = m.winfo_rootx() + m.winfo_width() - self.menu.winfo_reqwidth()
        self.menu.tk_popup(x, m.winfo_rooty() + m.winfo_height())

    def _key_allowed(self) -> bool:
        return time.monotonic() >= self._quiet_until

    def _apply_app_settings(self, app: config.AppSettings) -> None:
        self.app_settings = app
        if self._hotkey_tried or self.simulate or sys.platform != "win32":
            return
        self._hotkey_tried = True  # one attempt per run: changing app.hotkey needs a restart
        if not app.hotkey:
            self.hint.configure(text=" ")
            return
        try:
            mods, vk = config.parse_hotkey(app.hotkey)
        except ValueError as e:
            self.hint.configure(text="Hotkey off")
            self._set_detail(str(e))
            return
        self.hotkey = Hotkey(mods, vk, lambda: self.events.put(("hotkey",)))
        self.hotkey.start()
        self.hotkey.ready.wait(1.0)
        keys = " + ".join(k.strip().title() for k in app.hotkey.split("+"))
        if self.hotkey.ok:
            self.hotkey_text = keys
            self.hint.configure(text=f"or press {keys} anywhere")
        else:
            self.hotkey = None
            self.hint.configure(text=f"{keys} is taken by another program (see app.hotkey)")

    # -- worker plumbing ------------------------------------------------------------------------------

    def _start(self, job, *args, moves: bool = False, tune: bool = False) -> None:
        if self.busy:
            self._set_detail("Still working on the last request…")
            return
        if self.tune is not None and not tune:
            self._set_detail("Finish or stop fine-tuning first.")
            return
        self.busy = True
        self.moving = moves
        self.button.set_enabled(False)
        self.t_button.set_enabled(False)
        if moves:
            self.lens_picker.set_enabled(False)
        self._set_dot(BUSY)
        threading.Thread(target=self._work, args=(job, *args), daemon=True).start()

    def _work(self, job, *args) -> None:
        try:
            job(*args)
        except Cancelled:
            self.events.put(("status", "Cancelled", INFO))
            self.events.put(("detail", "Z did not move."))
        except session.NotAccepted:
            self.events.put(("status", "Not saved", INFO))
            self.events.put(
                ("detail", "Autofocus needs these focus heights. Press Autofocus to review them again.")
            )
        except Exception as e:  # noqa: BLE001 - every failure becomes a status line
            log.debug("operation failed", exc_info=True)
            if isinstance(e, autofocus.MotionError):
                self.events.put(("motion", False))
            self.events.put(("status", "Stopped", ERR))
            self.events.put(("detail", describe_error(e)))
            self.events.put(("beep", False))
        finally:
            self.events.put(("idle",))

    def _pump(self) -> None:
        try:
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(event)
                except Exception as e:  # noqa: BLE001 - one bad event must not stop the window
                    log.exception("event %r failed", event[0])
                    self._set_status("Something went wrong", ERR)
                    self._set_detail(describe_error(e))
        finally:
            self.root.after(50, self._pump)

    def _handle(self, event: tuple) -> None:
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
            _, question, default, answer, done = event
            try:
                answer.append(self._ask_yes_no(question, default=default))
            finally:
                done.set()  # an empty answer counts as "no"
        elif kind == "beep":
            if self.app_settings.sounds:
                _beep(event[1])
        elif kind == "hotkey":
            if self.tune is None:
                self.autofocus()
            elif self.tune["stage"] == "burn":
                self._tune_primary()  # while burning marks the hotkey means Next
            else:
                self._set_detail(
                    "Press Start in this window to begin."
                    if self.tune["stage"] == "intro"
                    else "Pick the marks, then Save or Done."
                )
                if self.app_settings.sounds:
                    _beep(False)
        elif kind == "tune_started" and self.tune is not None:
            _, dev, run, settings = event
            self.tune.update(dev=dev, run=run, settings=settings, stage="burn", index=0)
            self._tune_render()
        elif kind == "tune_pending" and self.tune is not None:
            self.tune["pending"] = event[1]
            if self.tune["stage"] == "burn":
                self._tune_render()
        elif kind == "tune_step" and self.tune is not None:
            self.tune["index"] = event[1]
            self._tune_render()
        elif kind == "tune_result" and self.tune is not None:
            dev, self.tune["dev"] = self.tune["dev"], None  # Z is back at focus: release the laser
            dev.__exit__(None, None, None)
            self.tune["stage"] = "result"
            self._tune_render()
        elif kind == "tune_end":
            if self.tune is not None:
                self._close_tune()
        elif kind == "motion":  # latch a motion fault until a move is seen to follow again
            ok, axis = event[1], event[2] if len(event) > 2 else None
            self.motion_fault = not ok
            self._verified_axis = axis if ok else None
            _save_state({**_load_state(self.simulate), "motion_fault": self.motion_fault}, self.simulate)
        elif kind == "idle":
            self.busy = False
            self.moving = False
            self.button.set_enabled(True)
            self.lens_picker.set_enabled(True)
            self._set_dot(self._tone)
            self.t_button.set_enabled(True)
            if self.tune is None:
                target = self.button
            elif self.tune["stage"] == "result":
                target = self.t_save if self.t_save.enabled else self.t_stop
            else:
                target = self.t_button
            if self.root.focus_get() in (None, self.button, self.t_button, self.t_save):
                target.focus_set()
            if self._lens_dirty:  # the lens was changed while busy: refresh the lens line now
                self._lens_dirty = False
                self._start(self._prepare_quietly)

    def _ask(self, question: str, *, default: str = "no") -> bool:
        """Ask from the worker thread; blocks until the user answers in the window."""
        answer: list[bool] = []
        done = threading.Event()
        self.events.put(("ask", question, default, answer, done))
        done.wait()
        return bool(answer and answer[0])

    def _ask_yes_no(self, question: str, *, default: str = "no") -> bool:
        from tkinter import messagebox

        self.root.deiconify()
        self.root.lift()
        _wait_for_keys_released()  # a held or repeated key must not answer the question unseen
        try:
            return messagebox.askyesno(self.title, question, parent=self.root, default=default)
        finally:
            self._quiet_until = time.monotonic() + self.QUIET_AFTER_DIALOG_S
            self.button.focus_set()

    def _set_status(self, text: str, tone: str = INFO) -> None:
        p = self.palette
        self.status.configure(text=text, fg={OK: p.ok, WARN: p.warn, ERR: p.err}.get(tone, p.text))
        self._tone = tone
        if tone != INFO or not self.busy:
            self._set_dot(tone)

    def _set_dot(self, tone: str) -> None:
        p = self.palette
        colour = {OK: p.ok, WARN: p.warn, ERR: p.err, BUSY: p.accent}.get(tone, p.info)
        self.dot.itemconfigure(self._dot, fill=colour)

    def _set_detail(self, text: str) -> None:
        self.detail.configure(text=text or " ")

    def _post(self, *event) -> None:
        self.events.put(event)

    # -- operations (worker thread) ---------------------------------------------------------------------

    def _settings(self, *, calibrate: bool = True) -> config.Settings:
        calibrated = calibrate or self._session.path.exists()

        def accept(found: config.Settings, source: str) -> bool:  # first use: ask before saving
            f = found.focus
            self._post("status", "First use", BUSY)
            from_laser = source == "the laser"
            return self._ask(
                (
                    "Use the focus heights stored in your laser?\n\n"
                    if from_laser
                    else "Use these focus heights?\n\n"
                )
                + f"Lens A ({f.field_a_mm:g} mm field): {f.target_a_mm:.1f} mm\n"
                + f"Lens B ({f.field_b_mm:g} mm field): {f.target_b_mm:.1f} mm\n\n"
                + (
                    "ComMarker measured these at the factory. "
                    if source == "the laser"
                    else f"They come from {source}. "
                )
                + "They are saved on this PC; you can fine-tune them later.",
            )

        s, why, _note = self._session.settings_with_lens(calibrate=calibrate, accept=accept)
        self._post("app", s.app)
        self._post("lens", lens_summary(self.lens_choice, why, s.focus.target_mm if calibrated else None))
        return s

    def _prepare(self) -> None:
        self._post("status", "Ready", INFO)
        self._post("detail", "Put the work piece under the head.")
        self._prepare_quietly()

    def _prepare_quietly(self) -> None:
        try:
            self._settings(calibrate=False)  # never touch the laser just because the app started
        except (ControllerError, OSError, ValueError) as e:
            self._post("detail", describe_error(e))
            try:
                s, _ = self._session.settings(calibrate=False)
                self._post("app", s.app)
            except (OSError, ValueError):
                self._post("app", config.AppSettings())

    def autofocus(self) -> None:
        self._start(self._autofocus, moves=True)

    def _autofocus(self) -> None:
        note = self._session.check_other_software()
        self._post("status", "Measuring…", BUSY)
        self._post("detail", note or "Reading the height sensor…")
        s = self._settings()

        dev, ctl = self._session.open()
        with dev:
            result = autofocus.run(
                ctl,
                s,
                passes=2,
                confirm=self._confirmer(s),
                on_plan=self._on_plan,
                probe=s.z_axis != self._verified_axis,
            )
        if result.verified:
            self._post("motion", True, s.z_axis)
        headline, detail, tone = describe_result(result)
        self._post("status", headline, tone)
        self._post("detail", detail)
        self._post("beep", tone == OK)

    def check_height(self) -> None:
        self._start(self._check_height)

    def _check_height(self) -> None:
        self._session.check_other_software()
        self._post("status", "Measuring…", BUSY)
        self._post("detail", "Reading the height sensor…")
        s = self._settings()
        dev, ctl = self._session.open()
        with dev:
            h = ctl.read_height_median(s.focus.samples)
        move = s.focus.target_mm - h
        self._post("status", f"Height {h:.1f} mm", INFO)
        if abs(move) < s.focus.deadband_mm:
            self._post("detail", f"Focus is at {s.focus.target_mm:.1f} mm: already in focus.")
        else:
            self._post(
                "detail", f"Focus is at {s.focus.target_mm:.1f} mm. Autofocus would move {updown(move)}."
            )

    # -- menu actions (UI thread) -----------------------------------------------------------------------

    def _lens_changed(self, choice: str) -> None:
        # Set here, on the UI thread; the worker only reads the session.
        self.lens_choice = choice
        self._session.lens_override = LENS_CHOICES.get(choice)
        _save_state({**_load_state(self.simulate), "lens": choice}, self.simulate)
        if self.busy:
            self._lens_dirty = True
        else:
            self._start(self._prepare_quietly)

    def _toggle_on_top(self) -> None:
        self.root.attributes("-topmost", self.on_top.get())

    def _toggle_autostart(self) -> None:
        try:
            set_start_with_windows(self.autostart.get())
        except OSError as e:
            self.autostart.set(not self.autostart.get())
            self._set_status("Could not change that", ERR)
            self._set_detail(f"The startup setting could not be changed: {e}")

    # -- fine-tuning with test burns (UI thread unless noted) --------------------------------------------

    def _build_tune(self, parent, p, sc: float, wrap: int, font: str) -> None:
        import tkinter as tk

        from . import ui

        self.t_steps = tk.Frame(parent, bg=p.bg)  # the Start/Next view; swapped for t_result at the end
        self.t_steps.pack(fill="x")
        steps = self.t_steps
        self.t_big = tk.Label(steps, text=" ", bg=p.bg, fg=p.text, font=(font, 26, "bold"))
        self.t_big.pack(pady=(int(10 * sc), 0))
        self.t_sub = tk.Label(steps, text=" ", bg=p.bg, fg=p.muted, font=(font, 9), wraplength=wrap)
        self.t_sub.pack()
        self._dot_size, self._dot_gap = int(8 * sc), int(14 * sc)
        self.t_dots = tk.Canvas(steps, width=wrap, height=int(14 * sc), bg=p.bg, highlightthickness=0, bd=0)
        self.t_dots.pack(pady=(int(8 * sc), 0))
        self.t_button = ui.RoundButton(
            steps, text="Start", command=self._tune_primary, palette=p, scale=sc, width=self.WIDTH, height=46,
            guard=self._key_allowed,
        )  # fmt: skip
        self.t_button.pack(pady=(int(12 * sc), int(6 * sc)))
        self.t_hint = tk.Label(steps, text=" ", bg=p.bg, fg=p.muted, font=(font, 9))
        self.t_hint.pack()

        self.t_result = tk.Frame(parent, bg=p.bg)  # shown on the result step
        labels = [fmt_offset(o) for o in TUNE_OFFSETS]
        self.t_low = ui.Segmented(
            self.t_result, options=labels, value=labels[0], command=lambda _: self._tune_outcome(),
            palette=p, scale=sc, pad=5,
        )  # fmt: skip
        self.t_high = ui.Segmented(
            self.t_result, options=labels, value=labels[-1], command=lambda _: self._tune_outcome(),
            palette=p, scale=sc, pad=5,
        )  # fmt: skip
        for text, picker in (("Lowest mark that still looks good", self.t_low), ("Highest", self.t_high)):
            tk.Label(self.t_result, text=text, bg=p.bg, fg=p.text, font=(font, 9), anchor="w").pack(fill="x")
            picker.pack(anchor="w", pady=(int(2 * sc), int(8 * sc)))
        self.t_outcome = tk.Label(
            self.t_result, text=" ", bg=p.bg, fg=p.text, font=(font, 9, "bold"), anchor="w", justify="left",
            wraplength=wrap,
        )  # fmt: skip
        self.t_outcome.pack(fill="x")
        self.t_save = ui.RoundButton(
            self.t_result, text="Save", command=self._tune_save, palette=p, scale=sc, width=self.WIDTH,
            height=40,
            guard=self._key_allowed,
        )  # fmt: skip
        self.t_save.pack(pady=(int(10 * sc), int(4 * sc)))

        self.t_stop = tk.Label(
            parent, text=" ", bg=p.bg, fg=p.accent, font=(font, 9, "underline"), cursor="hand2"
        )
        self.t_stop.configure(takefocus=1, highlightthickness=2, highlightbackground=p.bg)
        ui.focus_ring(self.t_stop, p.text, lambda: p.bg)
        for seq in ("<Button-1>", "<space>", "<Return>"):
            self.t_stop.bind(seq, lambda e: self._tune_secondary())
        self.t_stop.pack(pady=(int(6 * sc), 0))

    def open_fine_tune(self) -> None:
        if self.busy or self.tune is not None:
            self._set_detail("Wait for the current task to finish, then open fine-tuning.")
            return
        self.tune = {"stage": "intro", "index": 0, "burned": [], "dev": None, "run": None, "settings": None}
        self.main_part.pack_forget()
        self.tune_part.pack(fill="x")
        self._tune_render()

    def _close_tune(self) -> None:
        """Back to the Autofocus view; releases the laser if fine-tuning still holds it."""
        tune, self.tune = self.tune, None
        if tune and tune["dev"] is not None:
            try:
                tune["dev"].__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - releasing must not fail the window
                log.debug("closing the laser failed", exc_info=True)
        self.tune_part.pack_forget()
        self.main_part.pack(fill="x")
        self.button.set_enabled(not self.busy)
        self.button.focus_set()

    def _tune_render(self) -> None:
        t, n = self.tune, len(TUNE_OFFSETS)
        if t is None:
            return
        stage = t["stage"]
        if stage == "result":
            self.t_steps.pack_forget()
            self.t_result.pack(fill="x", before=self.t_stop)
        else:
            self.t_result.pack_forget()
            self.t_steps.pack(fill="x", before=self.t_stop)
        if stage == "intro":
            self._set_status("Fine-tune focus", INFO)
            self._set_detail(
                f"{n} test marks around focus. In LightBurn, set up a small design at low power."
            )
            self.t_big.configure(text=f"{fmt_offset(TUNE_OFFSETS[0])} … {fmt_offset(TUNE_OFFSETS[-1])} mm")
            self.t_sub.configure(text="Autofocus first, then one mark per height")
            self.t_button.set_text("Start")
            self.t_stop.configure(text="Cancel")
        elif stage == "burn" and t.get("pending"):
            # Z is still on its way to the first mark (or waiting for the laser): no burn wording.
            self.t_big.configure(text=f"{fmt_offset(TUNE_OFFSETS[0])} mm")
            self.t_sub.configure(text="Z is not at this height yet · do not burn")
            self.t_button.set_text("Continue")
            self.t_stop.configure(text="Stop and return Z to focus")
        elif stage == "burn":
            i = t["index"]
            self._set_status(f"Burn mark {i + 1} of {n}", INFO)
            self._set_detail("Burn the design at a fresh spot in LightBurn, then press Next.")
            self.t_big.configure(text=f"{fmt_offset(TUNE_OFFSETS[i])} mm")
            self.t_sub.configure(text="from the autofocus height · label the mark with it")
            self.t_button.set_text("Next" if i + 1 < n else "Done")
            self.t_stop.configure(text="Stop and return Z to focus")
        elif stage == "result":
            self._set_status("Pick the best marks", INFO)
            self._set_detail("Compare the marks with a loupe or a zoomed photo.")
            self._tune_outcome()
        self.t_hint.configure(
            text=f"or press {self.hotkey_text} in LightBurn"
            if self.hotkey_text and stage == "burn" and not t.get("pending")
            else " "
        )
        self._draw_dots()

    def _draw_dots(self) -> None:
        c, p, n = self.t_dots, self.palette, len(TUNE_OFFSETS)
        c.delete("all")
        t = self.tune or {}
        done, current = len(t.get("burned", [])), t.get("index", -1) if t.get("stage") == "burn" else -1
        d, gap = self._dot_size, self._dot_gap
        x0 = (int(c.cget("width")) - (n * d + (n - 1) * (gap - d))) / 2
        for i in range(n):
            x = x0 + i * gap
            fill = p.accent if i < done else p.bg
            outline = p.accent if i <= max(done - 1, current) else p.border
            c.create_oval(x, 2, x + d, 2 + d, fill=fill, outline=outline, width=2 if i == current else 1)

    def _tune_primary(self) -> None:
        """Start, or Next after a mark has been burned (also the hotkey while fine-tuning)."""
        t = self.tune
        if t is None or self.busy:
            return
        if t["stage"] == "intro":
            self._start(self._tune_begin, moves=True, tune=True)
        elif t["stage"] == "burn" and t.get("pending"):
            self._start(self._tune_approach, t["run"], moves=True, tune=True)
        elif t["stage"] == "burn":
            if t["index"] not in t["burned"]:
                t["burned"].append(t["index"])
            self._start(self._tune_advance, t["run"], t["index"], moves=True, tune=True)

    def _tune_secondary(self) -> None:
        """Cancel / Stop and return / Keep, depending on the step."""
        t = self.tune
        if t is None or self.busy:
            return
        if t["stage"] == "burn" and t["run"] is not None:
            self._start(self._tune_stop, t["run"], moves=True, tune=True)
            return
        self._close_tune()
        self._set_status("Ready", INFO)
        self._set_detail(
            "Focus height unchanged." if t["stage"] == "result" else "Put the work piece under the head."
        )

    def _tune_outcome(self) -> tuple[float, float] | None:
        t = self.tune
        if t is None or t["settings"] is None or t["run"] is None:
            return None
        lo = parse_offset_label(self.t_low.value)
        hi = parse_offset_label(self.t_high.value)
        centre, new = ladder.best_focus(t["run"].reference, lo, hi)
        f = t["settings"].focus
        old = f.target_b_mm if f.lens.lower() == "b" else f.target_a_mm
        ok = f.sensor_min_mm <= new <= f.sensor_max_mm
        edge = (
            " The good range reaches the end of the ladder."
            if min(lo, hi) == TUNE_OFFSETS[0] or max(lo, hi) == TUNE_OFFSETS[-1]
            else ""
        )
        lands = ""
        if f.offset_mm:
            goes = new + f.offset_mm
            lands = f" With focus.offset_mm ({f.offset_mm:+g} mm) on top, autofocus goes to {goes:.1f} mm."
        if not ok:
            text = f"{new:.1f} mm is outside the sensor range; check the marks again."
        elif round(new - old, 1) == 0:
            text = f"Best focus matches the saved height ({old:.1f} mm).{lands}{edge}"
        else:
            heights = f"lens {f.lens.upper()} focus height {old:.1f} → {new:.1f} mm"
            text = f"Best focus {centre:+.1f} mm → {heights}.{lands}{edge}"
        self.t_outcome.configure(text=text)
        change = ok and round(new - old, 1) != 0
        self.t_save.set_text(f"Save {new:.1f} mm" if change else "Nothing to save")
        self.t_save.set_enabled(change)
        self.t_stop.configure(text="Keep the current focus height" if change else "Done")
        return old, new

    def _tune_save(self) -> None:
        t, outcome = self.tune, self._tune_outcome()
        if t is None or outcome is None:
            return
        old, new = outcome
        lens_key = t["settings"].focus.lens.lower()
        try:
            ladder.save_focus_target(self._session.path, lens_key, new)
        except (OSError, ValueError) as e:
            self._set_status("Not saved", ERR)
            self._set_detail(str(e))
            return
        self._close_tune()
        self._set_status("Focus saved", OK)
        self._set_detail(f"Lens {lens_key.upper()} focus height {old:.1f} → {new:.1f} mm.")
        self._start(self._prepare_quietly)

    # worker thread ----------------------------------------------------------------------------------

    def _tune_begin(self) -> None:
        self._session.check_other_software()
        self._post("status", "Autofocus first…", BUSY)
        self._post("detail", "Reading the height sensor…")
        s = self._settings()
        if ladder.reach_mm(TUNE_OFFSETS) > s.focus.max_move_mm:  # before anything moves
            raise autofocus.FocusError(
                f"fine-tuning needs {ladder.reach_mm(TUNE_OFFSETS):g} mm of travel but focus.max_move_mm is "
                f"{s.focus.max_move_mm:g}"
            )
        dev, ctl = self._session.open()  # held until fine-tuning ends: no other program can move Z
        run = None
        try:
            result = autofocus.run(
                ctl, s, passes=2, confirm=self._confirmer(s), on_plan=self._on_plan,
                probe=s.z_axis != self._verified_axis,
            )  # fmt: skip
            if result.verified:
                self._post("motion", True, s.z_axis)
            if result.outcome is autofocus.Outcome.CANCELLED:
                raise Cancelled
            if result.outcome is not autofocus.Outcome.IN_FOCUS:
                raise autofocus.FocusError(describe_result(result)[1].rstrip("."))
            run = ladder.Ladder(ctl, s, list(TUNE_OFFSETS))
            run.measure_reference()
        except BaseException:
            dev.__exit__(None, None, None)
            self._post("tune_end")
            raise
        self._post("tune_pending", True)  # not at the first mark yet: no burn instructions
        self._post("tune_started", dev, run, s)
        self._tune_approach(run)

    def _tune_approach(self, run: ladder.Ladder) -> None:
        """Go to the first mark (two moves: below it, then up into it). Busy keeps the run open."""
        try:
            self._post("status", f"Moving to {fmt_offset(run.offsets[0])} mm…", BUSY)
            run.go_to(run.offsets[0])
        except ControllerBusyError:
            self._post("tune_pending", True)
            self._post("status", "Laser busy", WARN)
            self._post("detail", "LightBurn is using the laser. When it is free, press Continue.")
            return
        except Exception as e:  # noqa: BLE001 - reported with where Z is
            self._tune_failed(e, run)
            return
        self._post("tune_pending", False)
        self._post("tune_step", 0)

    def _tune_advance(self, run: ladder.Ladder, index: int) -> None:
        try:
            if index + 1 < len(run.offsets):
                self._post("status", f"Moving to {fmt_offset(run.offsets[index + 1])} mm…", BUSY)
                run.go_to(run.offsets[index + 1])
                self._post("tune_step", index + 1)
            else:
                self._post("status", "Returning to focus…", BUSY)
                run.return_to_focus()
                self._post("tune_result")
        except ControllerBusyError:
            self._post("status", "Laser busy", WARN)
            self._post(
                "detail", "LightBurn is still using the laser. When the mark is done, press Next again."
            )
        except Exception as e:  # noqa: BLE001 - reported with where Z is
            self._tune_failed(e, run)

    def _tune_stop(self, run: ladder.Ladder) -> None:
        try:
            self._post("status", "Returning to focus…", BUSY)
            run.return_to_focus()
        except ControllerBusyError:
            self._post("status", "Laser busy", WARN)
            self._post("detail", "LightBurn is still using the laser. Stop it, then press Stop again.")
            return
        except Exception as e:  # noqa: BLE001 - reported with where Z is
            self._tune_failed(e, run)
            return
        self._post("tune_end")
        self._post("status", "Ready", INFO)
        self._post("detail", "Fine-tuning stopped. Z is back at the focus height.")

    def _tune_failed(self, e: Exception, run: ladder.Ladder) -> None:
        """End fine-tuning after a fault without moving Z again, and say where Z was left."""
        log.debug("fine-tuning failed", exc_info=True)
        if isinstance(e, autofocus.MotionError):
            self._post("motion", False)
        self._post("tune_end")
        self._post("status", "Stopped", ERR)
        self._post("detail", f"{describe_error(e)} {run.where()}. Check the Z axis, then press Autofocus.")
        self._post("beep", False)

    def _confirmer(self, s: config.Settings):
        """The app's confirmation rule for a focus run (worker thread)."""
        fault = self.motion_fault

        def confirm(p: autofocus.FocusPlan) -> bool:
            if needs_confirmation(p, s.app, motion_fault=fault):
                self._post("status", "Confirm the move", BUSY)
                towards = ", towards the work" if p.move_mm < 0 else ""
                warning = "Last time Z did not move as expected.\n\n" if fault else ""
                if not self._ask(
                    f"{warning}Move the head {updown(p.move_mm)}{towards}?\n\n"
                    f"Height {p.height_mm:.1f} mm, focus at {p.target_mm:.1f} mm."
                ):
                    return False
            self._post("status", f"Moving {updown(p.move_mm)}…", BUSY)
            return True

        return confirm

    def _on_plan(self, i: int, p: autofocus.FocusPlan) -> None:
        self._post("detail", f"Height {p.height_mm:.1f} mm · focus {p.target_mm:.1f} mm")

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

        self._post("status", "Waiting for the driver installer…", BUSY)
        driver.run_installer(installer)
        self._post("status", "Driver installer finished", WARN)
        self._post("detail", "Unplug and replug the laser's USB cable.")

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

        messagebox.showinfo(self.title, text, parent=self.root)


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
        if _already_running(MUTEX_NAME + ("Simulation" if args.simulate else "")):
            _show_existing_window(window_title(args.simulate))
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
