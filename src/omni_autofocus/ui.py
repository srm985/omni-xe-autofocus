"""Look and feel of the app window: a light/dark palette that follows Windows, a rounded accent
button and a segmented control. Plain tkinter (Canvas and Label), no extra packages."""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from dataclasses import dataclass

FONT = "Segoe UI"


@dataclass(frozen=True)
class Palette:
    bg: str
    card: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_disabled: str
    on_accent: str
    ok: str
    warn: str
    err: str
    info: str


LIGHT = Palette(
    bg="#f3f4f6",
    card="#ffffff",
    border="#e3e5e8",
    text="#1f2328",
    muted="#6b7280",
    accent="#1859a0",
    accent_hover="#1f6bc0",
    accent_pressed="#134a86",
    accent_disabled="#9fb5d1",
    on_accent="#ffffff",
    ok="#1a7f37",
    warn="#9a6700",
    err="#cf222e",
    info="#6b7280",
)

DARK = Palette(
    bg="#1b1c1f",
    card="#26282c",
    border="#34363b",
    text="#e8e9eb",
    muted="#9aa0a8",
    accent="#3b82e0",
    accent_hover="#5596ea",
    accent_pressed="#2b6cc4",
    accent_disabled="#33475f",
    on_accent="#ffffff",
    ok="#3fb950",
    warn="#d29922",
    err="#f85149",
    info="#9aa0a8",
)


def windows_prefers_dark() -> bool:
    forced = os.environ.get("OMNI_AUTOFOCUS_THEME", "").lower()  # "light"/"dark", for screenshots
    if forced in ("light", "dark"):
        return forced == "dark"
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def dark_title_bar(root: tk.Tk, dark: bool) -> None:
    """Match the Windows title bar to the theme (Windows 10 20H1+ / 11; ignored elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def scale_of(root: tk.Misc) -> float:
    """Screen scaling relative to 96 dpi, for sizes given in 'design pixels'."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


class RoundButton(tk.Canvas):
    """A filled, rounded button drawn on a canvas: hover, pressed and disabled states, Space/Enter
    activate it when it has keyboard focus."""

    def __init__(
        self, parent, *, text: str, command, palette: Palette, scale: float, width: int, height: int
    ):
        self.p, self.s = palette, scale
        w, h = int(width * scale), int(height * scale)
        super().__init__(
            parent,
            width=w,
            height=h,
            bg=parent["bg"],
            highlightthickness=0,
            bd=0,
            takefocus=1,
            cursor="hand2",
        )
        self.command = command
        self.enabled = True
        self._hover = self._pressed = False
        r = int(16 * scale)
        self._shape = self._rounded(1, 1, w - 1, h - 1, r, fill=palette.accent, outline="")
        self._label = self.create_text(
            w / 2, h / 2, text=text, fill=palette.on_accent, font=(FONT, 13, "bold")
        )
        self._focus_ring = self._rounded(3, 3, w - 3, h - 3, r - 2, fill="", outline="", width=1)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self.itemconfigure(self._focus_ring, outline=palette.on_accent))
        self.bind("<FocusOut>", lambda e: self.itemconfigure(self._focus_ring, outline=""))
        self.bind("<space>", lambda e: self._invoke())

    def _rounded(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]  # fmt: skip
        return self.create_polygon(pts, smooth=True, **kw)

    def _set(self, *, hover: bool | None = None, pressed: bool | None = None) -> None:
        if hover is not None:
            self._hover = hover
        if pressed is not None:
            self._pressed = pressed
        p = self.p
        fill = (
            p.accent_disabled
            if not self.enabled
            else p.accent_pressed
            if self._pressed
            else p.accent_hover
            if self._hover
            else p.accent
        )
        self.itemconfigure(self._shape, fill=fill)

    def _release(self, event) -> None:
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._set(pressed=False)
        if inside:
            self._invoke()

    def _invoke(self) -> None:
        if self.enabled:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._set()

    def set_text(self, text: str) -> None:
        self.itemconfigure(self._label, text=text)

    @property
    def text(self) -> str:
        return self.itemcget(self._label, "text")


class Segmented(tk.Frame):
    """A row of mutually exclusive options, like a toggle group."""

    def __init__(self, parent, *, options: list[str], value: str, command, palette: Palette):
        super().__init__(parent, bg=palette.border, padx=1, pady=1)
        self.p, self.command = palette, command
        self.value = value
        self._labels: dict[str, tk.Label] = {}
        for i, option in enumerate(options):
            lbl = tk.Label(self, text=option, font=(FONT, 9), padx=12, pady=3, cursor="hand2", takefocus=1)
            lbl.grid(row=0, column=i, padx=(0 if i == 0 else 1, 0))
            lbl.bind("<Button-1>", lambda e, o=option: self.select(o, notify=True))
            lbl.bind("<space>", lambda e, o=option: self.select(o, notify=True))
            self._labels[option] = lbl
        self._paint()

    def select(self, option: str, *, notify: bool = False) -> None:
        changed = option != self.value
        self.value = option
        self._paint()
        if notify and changed:
            self.command(option)

    def _paint(self) -> None:
        for option, lbl in self._labels.items():
            on = option == self.value
            lbl.configure(
                bg=self.p.accent if on else self.p.card,
                fg=self.p.on_accent if on else self.p.text,
                font=(FONT, 9, "bold" if on else "normal"),
            )
